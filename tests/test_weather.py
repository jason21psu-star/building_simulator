"""Weather station, psychrometrics, and weather sharing across simulators."""

import asyncio

import pytest
from bacpypes3.primitivedata import Real

from hvacsim import bacnet_io, psychro
from hvacsim.config import load_host_config
from hvacsim.mockalc import MockController
from hvacsim.models.weather_station import WeatherStation
from hvacsim.runner import DEMO_LAN, Host


# ----- psychrometrics (ASHRAE chart values) ---------------------------------


@pytest.mark.parametrize("t,rh,w,h,dp,wb", [
    (75, 50, 0.00923, 28.1, 55.1, 62.6),
    (55, 90, 0.00826, 22.2, 52.1, 53.3),
    (20, 60, 0.00128, 6.2, 9.6, 17.1),
])
def test_psychro_states(t, rh, w, h, dp, wb):
    s = psychro.state(t, rh, psychro.p_atm(0))
    assert s["w"] == pytest.approx(w, abs=0.0001)
    assert s["h"] == pytest.approx(h, abs=0.15)
    assert s["dewpoint"] == pytest.approx(dp, abs=0.2)
    assert s["wetbulb"] == pytest.approx(wb, abs=0.2)


def test_psychro_wetbulb_roundtrip_and_altitude():
    p = psychro.p_atm(0)
    w = psychro._w_from_wetbulb(95, 75, p)
    assert w == pytest.approx(0.0141, abs=0.0002)
    assert psychro.wetbulb(95, w, p) == pytest.approx(75, abs=0.05)
    assert psychro.p_atm(5280) == pytest.approx(12.1, abs=0.05)
    # same RH holds more moisture at altitude
    assert psychro.w_from_rh(75, 50, psychro.p_atm(5280)) > psychro.w_from_rh(75, 50, p)


# ----- the station model ----------------------------------------------------


def test_station_readings_and_faults():
    m = WeatherStation({"oat": 90.0, "oa_rh": 40.0}, seed=1)
    m.step(1)
    s = m.signals
    assert s["oa_temp"] == pytest.approx(90.0, abs=0.3)
    assert s["oa_enthalpy"] == pytest.approx(psychro.state(90, 40, psychro.p_atm(0))["h"], abs=0.3)
    m.set_fault("solar_error", True, 8.0)
    m.step(1)
    assert m.signals["oa_temp"] == pytest.approx(98.0, abs=0.3)
    assert m.signals["oa_temp_actual"] == 90.0          # the physics doesn't change
    assert m.signals["oa_rh"] < 38.0                    # RH element in the same hot shield reads low
    assert m.signals["oa_dewpoint"] == pytest.approx(psychro.state(90, 40, psychro.p_atm(0))["dewpoint"], abs=0.7)
    assert m.signals["oa_enthalpy"] > m.signals["oa_enthalpy_actual"]   # derived values follow the bad sensor
    m.set_fault("rh_sensor_failed", True, 0.0)
    m.step(1)
    assert m.signals["oa_rh"] == 0.0


def test_ramp_rate():
    m = WeatherStation({"oat": 50.0, "ramp_rate": 1.0}, seed=1)
    m.set_param("oat", 60.0)
    m.step(300)                                          # 5 min at 1 °F/min
    assert m.signals["oa_temp_actual"] == pytest.approx(55.0, abs=0.01)


# ----- host integration -----------------------------------------------------


def wait_for(pred, timeout=8.0):
    async def _w():
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if pred():
                return True
            await asyncio.sleep(0.1)
        return pred()
    return _w()


def test_weather_shared_and_read_over_bacnet():
    asyncio.run(_host())


async def _host():
    cfg = load_host_config("configs/station-1.yaml")
    cfg["instances"] = [i for i in cfg["instances"] if i["name"] in ("WX-1", "UH-1-SIM")]
    host = Host(cfg, demo=True, demo_controllers=False)
    wx, uh = host.unit("WX-1"), host.unit("UH-1-SIM")
    assert host.units[0] is wx and uh.weather is wx and uh.weather_params == {"oat": "oa_temp_actual"}
    alc = MockController(240110, bacnet_io.virtual_port(1, DEMO_LAN, host.ip_network, 0x20), name="ALC")
    task = asyncio.create_task(host.run())
    try:
        await asyncio.sleep(0.3)
        addr = f"5001:{wx.cfg['mac']}"
        wx.model.set_param("oat", 12.0)
        wx.model.set_param("oa_rh", 70.0)
        assert await wait_for(lambda: uh.model.p["oat"] == 12.0, 3)   # unit heater physics follows
        t = await alc.app.read_property(addr, "analog-input,1", "present-value")
        rh = await alc.app.read_property(addr, "analog-input,2", "present-value")
        name = await alc.app.read_property(addr, "analog-input,1", "object-name")
        assert float(t) == pytest.approx(12.0, abs=0.3) and float(rh) == pytest.approx(70.0, abs=1.5)
        assert str(name) == "OA TEMP"
        # read-only: the ALC can't write the station's values
        with pytest.raises(BaseException):
            await alc.app.write_property(addr, "analog-input,1", "present-value", Real(99.0))

        # offline: the device stops answering
        wx.model.set_fault("device_offline", True)
        await asyncio.sleep(0.3)
        with pytest.raises(BaseException):
            await asyncio.wait_for(alc.app.read_property(addr, "analog-input,1", "present-value"), 4)
        wx.model.set_fault("device_offline", False)
        await asyncio.sleep(0.3)
        t = await asyncio.wait_for(alc.app.read_property(addr, "analog-input,1", "present-value"), 6)
        assert float(t) == pytest.approx(12.0, abs=0.3)
    finally:
        task.cancel()
        alc.app.close()
        host.close()
