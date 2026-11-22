// Equipment view: weather station
(function () {
  // Magnus saturation pressure (psia) - accurate enough for drawing the chart
  const pws = t => { const c = (t - 32) / 1.8; return 0.0886 * Math.exp(17.62 * c / (243.12 + c)); };
  const W = (t, rh, p) => { const pw = rh / 100 * pws(t); return 0.621945 * pw / (p - pw); };
  const X0 = 0, X1 = 110, Y1 = 0.028;              // chart range: dry bulb °F, humidity ratio lb/lb
  const sx = t => 40 + (t - X0) / (X1 - X0) * 400, sy = w => 230 - w / Y1 * 210;

  function chart(p) {
    let out = '';
    for (const rh of [20, 40, 60, 80, 100]) {
      let d = '';
      for (let t = X0; t <= X1; t += 2) {
        const w = W(t, rh, p); if (w > Y1) break;
        d += (d ? ' L' : 'M') + sx(t).toFixed(1) + ',' + sy(w).toFixed(1);
      }
      out += `<path d="${d}" fill="none" stroke="${rh === 100 ? 'var(--ink)' : 'var(--line)'}" stroke-width="${rh === 100 ? 1.6 : 1}"/>`;
    }
    for (let t = 0; t <= 110; t += 10) out += `<text x="${sx(t)}" y="246" text-anchor="middle">${t}</text>`;
    for (const g of [0, 50, 100, 150, 196]) out += `<text x="446" y="${sy(g / 7000) + 4}">${g}</text>`;
    return out;
  }

  VIEWS.weather_station = {
    html: `
    <style>
      .wx { display:grid; grid-template-columns: minmax(0,1fr) minmax(0,1.3fr); gap:14px; align-items:start; }
      @media (max-width: 760px) { .wx { grid-template-columns: 1fr; } }
      .wxbig { display:grid; grid-template-columns: 1fr 1fr; gap:10px; }
      .wxtile { background: var(--chip); border-radius: 10px; padding: 10px 12px; }
      .wxtile .v { font-size: 28px; font-weight: 600; font-variant-numeric: tabular-nums; }
      .wxtile .l { color: var(--muted); font-size: 12px; }
      .wxtile .t { color: var(--muted); font-size: 12px; }
      .wxsvg text { font: 10px system-ui, sans-serif; fill: var(--muted); }
    </style>
    <div class="wx">
      <div>
        <div class="wxbig">
          <div class="wxtile"><div class="l">OA temperature (sensor)</div><div class="v" id="wx_t"></div><div class="t" id="wx_tt"></div></div>
          <div class="wxtile"><div class="l">OA humidity (sensor)</div><div class="v" id="wx_rh"></div><div class="t" id="wx_rht"></div></div>
        </div>
        <table style="margin-top:10px"><tbody id="wx_tbl"></tbody></table>
        <p class="muted" id="wx_users" style="margin-top:10px"></p>
      </div>
      <svg class="wxsvg" viewBox="0 0 470 255" role="img" aria-label="Psychrometric chart">
        <g id="wx_chart"></g>
        <text x="240" y="12" text-anchor="middle">dry bulb °F (x) · grains/lb (y) · RH lines 20-100 %</text>
        <circle id="wx_true" r="6" fill="var(--accent)"/>
        <circle id="wx_meas" r="5" fill="none" stroke="var(--bad)" stroke-width="2"/>
        <text x="48" y="30" id="wx_legend"></text>
      </svg>
    </div>`,

    render(s) {
      const g = s.signals, $ = id => document.getElementById(id);
      const chartEl = $('wx_chart');
      if (!chartEl.dataset.p || chartEl.dataset.p !== String(g.baro_psia)) {
        chartEl.innerHTML = chart(g.baro_psia); chartEl.dataset.p = String(g.baro_psia);
      }
      $('wx_t').textContent = g.oa_temp.toFixed(1) + ' °F';
      $('wx_tt').textContent = 'actual ' + g.oa_temp_actual.toFixed(1) + ' °F';
      $('wx_rh').textContent = g.oa_rh.toFixed(1) + ' %';
      $('wx_rht').textContent = 'actual ' + g.oa_rh_actual.toFixed(1) + ' %';
      const rows = [
        ['Dew point', g.oa_dewpoint.toFixed(1) + ' °F'], ['Wet bulb', g.oa_wetbulb.toFixed(1) + ' °F'],
        ['Enthalpy', g.oa_enthalpy.toFixed(2) + ' Btu/lb'], ['Humidity ratio', g.oa_grains.toFixed(1) + ' gr/lb'],
        ['Actual enthalpy', g.oa_enthalpy_actual.toFixed(2) + ' Btu/lb'], ['Barometric pressure', g.baro_psia.toFixed(3) + ' psia'],
      ];
      $('wx_tbl').innerHTML = rows.map(([k, v]) => `<tr><td>${k}</td><td class="v">${v}</td></tr>`).join('');
      const users = s.host.units.filter(u => u.uses_weather).map(u => u.name);
      $('wx_users').textContent = users.length ? 'Outdoor conditions used by: ' + users.join(', ') : 'No other simulators use this station.';
      const tp = (t, rh) => [sx(Math.max(X0, Math.min(X1, t))), sy(Math.min(Y1, W(Math.max(X0, Math.min(X1, t)), Math.max(0, rh), g.baro_psia)))];
      const [ax, ay] = tp(g.oa_temp_actual, g.oa_rh_actual), [mx, my] = tp(g.oa_temp, g.oa_rh);
      $('wx_true').setAttribute('cx', ax); $('wx_true').setAttribute('cy', ay);
      $('wx_meas').setAttribute('cx', mx); $('wx_meas').setAttribute('cy', my);
      $('wx_legend').textContent = '● actual   ○ sensor reading';
    },
  };
})();
