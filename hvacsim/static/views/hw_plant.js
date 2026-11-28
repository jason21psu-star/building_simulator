// Equipment view: hot water plant (boilers, pumps, decoupler, branches)
(function () {
  const f0 = v => (v == null || isNaN(v) ? '—' : Math.round(Number(v)).toLocaleString());
  const f1 = v => (v == null || isNaN(v) ? '—' : Number(v).toFixed(1));
  const pill = (txt, cls) => `<span class="pill ${cls || ''}">${txt}</span>`;
  const btn = (action, label, value) => `<button onclick="keypad('${action}'${value !== undefined ? ',' + JSON.stringify(value) : ''})">${label}</button>`;
  // vendor-neutral equipment rows (docs/EQUIPMENT_INTERFACE_DESIGN.md, section 7): each unit publishes
  // pre.view = {short, protocol, integration, text, state, codes, fields, panel, common}
  const vview = (g, p) => { try { return JSON.parse(g[p + '.view'] || '{}'); } catch (e) { return {}; } };
  const vcls = st => ({ lockout: 'err', no_power: 'err', alarm: 'warn', running: 'on', starting: 'warn', stopping: 'warn' })[st] || '';
  const vheading = (views, noun) => {
    const names = [...new Set(views.map(v => v.short).filter(Boolean))];
    const protos = [...new Set(views.map(v => ({ bacnet: 'BACnet', modbus_tcp: 'Modbus TCP' })[v.protocol]).filter(Boolean))];
    return `${noun} (${names.join(', ') || '—'}${protos.length ? ', ' + protos.join(' + ') : ''})`;
  };
  const vstatus = v => `${pill((v.text || '').split(' · ')[0], vcls(v.state))}<div class="muted" style="font-size:12px">${(v.codes || []).join('<br>')}</div>`;
  const vfields = v => (v.fields || []).map(([k, x]) => `<span class="muted">${k}</span> ${x}`).join('<br>');
  const vpanel = (p, v) => (v.panel || []).map(([k, label]) => btn(p + '.' + k, label)).join('');

  VIEWS.hw_plant = {
    wide: true,
    html: `
    <style>
      .hp h3 { font-size: 13px; margin: 14px 0 6px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
      .hp table td, .hp table th { padding: 4px 5px; font-size: 13px; }
      .hp .kpis { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
      .hp .kpi { background: var(--chip); border-radius: 8px; padding: 8px 10px; }
      .hp .kpi b { display: block; font-size: 18px; font-variant-numeric: tabular-nums; }
      .hp .kpi span { color: var(--muted); font-size: 12px; }
      .hp .btns button { padding: 2px 7px; font-size: 12px; }
      .hp .scroll { overflow-x: auto; }
      .hp .pill { white-space: nowrap; }
      .hp td.st { max-width: 340px; }
      .hp .power { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 10px; }
      .hp .flame { display: inline-block; width: 60px; height: 8px; background: var(--line); border-radius: 4px; overflow: hidden; vertical-align: middle; }
      .hp .flame div { height: 100%; background: #e8742a; }
    </style>
    <div class="hp">
      <div class="power" id="hp_power"></div>
      <div class="kpis" id="hp_kpis"></div>
      <h3 id="hp_b_h">Boilers</h3><div class="scroll"><table><thead><tr>
        <th>Boiler</th><th>Display status</th><th>Firing</th><th>Supply / return °F</th><th>MBH out</th>
        <th>gpm · flow sw</th><th>Vendor</th><th>Display</th></tr></thead><tbody id="hp_b"></tbody></table></div>
      <h3>Pumps (ACH580, hardwired)</h3><div class="scroll"><table><thead><tr>
        <th>Pump</th><th>Drive</th><th>Hz</th><th>gpm</th><th>Head ft</th><th>Amps</th><th>Keypad</th></tr></thead><tbody id="hp_pumps"></tbody></table></div>
      <h3>Decoupler and loop</h3><div id="hp_dec"></div>
      <h3>Building branches</h3><div class="scroll"><table><thead><tr>
        <th>Branch</th><th>Load · met MBH</th><th>Flow gpm</th><th>Coil valves</th><th>Remote DP psi</th><th>Return °F</th>
        <th>End of branch °F</th><th>3-way valve</th><th>Connected gpm</th></tr></thead><tbody id="hp_br"></tbody></table></div>
      <h3>BACnet devices</h3><table><tbody id="hp_dev"></tbody></table>
    </div>`,

    render(s) {
      const g = s.signals, $ = id => document.getElementById(id);
      let L; try { L = JSON.parse(g.layout || '{}'); } catch (e) { L = {}; }
      const tag = p => (L.tags || {})[p] || p.toUpperCase();
      const h = s.host || {};
      $('hp_power').innerHTML = `Utility power ${g.utility_ok ? pill('NORMAL', 'on') : pill('FAILED', 'err')}
        ${h.restore_in != null ? `<span class="muted">restores in ${Math.ceil(h.restore_in / 60)} min</span>` : ''}
        <button onclick="hwPower('fail')">Fail utility</button>
        <label>for <input type="number" id="hp_minutes" min="0" step="1" style="width:70px" placeholder="min"></label>
        <button onclick="hwPower('restore')">Restore</button>
        · Boiler E-stop ${g.estop_pressed ? pill('PRESSED', 'err') : pill('normal', 'on')}
        ${btn('estop', 'Press E-stop')}${btn('estop_reset', 'Release')}
        <span class="muted">${g.sim_clock} · ${g.occupied ? 'occupied' : 'unoccupied'}${g.warmup ? ' · WARM-UP' : ''} · OAT ${f1(g.oat)}°F</span>`;
      const kpi = (v, label) => `<div class="kpi"><b>${v}</b><span>${label}</span></div>`;
      $('hp_kpis').innerHTML = [
        kpi(f0(g.building_mbh), 'building load MBH'), kpi(f0(g.plant_mbh), 'boiler output MBH'),
        kpi(f0(g.gas_mbh), 'gas input MBH'), kpi(f1(g.plant_eff) + ' %', 'plant efficiency'),
        kpi(f0(g.primary_flow), 'primary gpm'), kpi(f0(g.secondary_flow), 'secondary gpm'),
        kpi((g.dcplr_flow > 0 ? '+' : '') + f0(g.dcplr_flow), g.dcplr_flow >= 0 ? 'decoupler gpm (supply → return)' : 'decoupler gpm (return → supply)'),
        kpi(f1(g.phws_actual) + ' / ' + f1(g.shws_actual) + '°F', 'primary / secondary supply'),
        kpi(f1(g.phwr_actual) + ' / ' + f1(g.shwr_actual) + '°F', 'primary / secondary return'),
        kpi(f1(g.plant_dp) + ' psi', 'plant DP'), kpi(f0(g.plant_kw) + ' kW', 'pump power')].join('');

      const bv = (L.boilers || []).map(b => vview(g, b));
      $('hp_b_h').textContent = vheading(bv, 'Boilers');
      $('hp_b').innerHTML = (L.boilers || []).map((b, i) => {
        const v = bv[i], m = v.common || {};
        const rate = m.firing || 0;
        return `<tr><td><b>${tag(b)}</b><br><span class="muted">${v.short || ''}${v.state === 'local' ? ' · LOCAL' : ''}</span></td>
          <td class="st">${vstatus(v)}</td>
          <td class="v"><span class="flame"><div style="width:${Math.min(100, rate)}%"></div></span> ${f0(rate)}%</td>
          <td class="v">${f1(m.supply)} / ${f1(m.return)}</td>
          <td class="v">${f0(g[b + '.mbh_out'])}</td>
          <td class="v">${f0(g[b + '.flow'])} · ${m.flow_switch ? pill('made', 'on') : pill('open')}</td>
          <td style="font-size:12px">${vfields(v)}</td>
          <td class="btns">${vpanel(b, v)}</td></tr>`;
      }).join('');

      const pumpRow = p => {
        const st = (g[p + '.status'] || '').split('|');
        const cls = /FAULT|NO POWER/.test(st[1]) ? 'err' : /RUNNING/.test(st[1]) ? 'on' : '';
        return `<tr><td><b>${tag(p)}</b></td><td>${pill(st[1] || '', cls)} <span class="muted">${(st[0] || '').toUpperCase()}${st[4] ? ' · ' + st[4] : ''}</span></td>
          <td class="v">${f1(g[p + '.freq'])}</td><td class="v">${f0(g[p + '.flow'])}</td><td class="v">${f1(g[p + '.head'])}</td>
          <td class="v">${f1(g[p + '_amps'])}</td>
          <td class="btns">${btn(p + '.hand', 'Hand')}${btn(p + '.off', 'Off')}${btn(p + '.auto', 'Auto')}${btn(p + '.reset', 'Reset')}</td></tr>`;
      };
      $('hp_pumps').innerHTML = [...(L.primary_pumps || []), ...(L.secondary_pumps || [])].map(pumpRow).join('');

      const d = g.dcplr_flow || 0;
      $('hp_dec').innerHTML = `<table><tbody><tr>
        <td>Decoupler</td><td class="v"><b>${f0(Math.abs(d))} gpm</b> ${Math.abs(d) < 5 ? pill('balanced', 'on') : d > 0
          ? pill('supply → return: hot water short-circuits to the boilers', 'warn') : pill('return → supply: return water mixes into the building supply', 'warn')}</td></tr>
        <tr><td>Loop pressure</td><td class="v">${f1(g.loop_press)} psig ${g.relief_lifting ? pill('RELIEF LIFTING', 'err') : ''}</td></tr>
        <tr><td>Boiler room</td><td class="v">${f1(g.room_temp)}°F · louver ${f0(g.louver_pos)}% ${g.louver_open ? pill('proven', 'on') : pill('not proven')}
          · gas ${g.gas_press_ok ? pill('normal', 'on') : pill('ALARM', 'err')}</td></tr></tbody></table>`;

      $('hp_br').innerHTML = (L.branches || []).map(b => `<tr><td><b>${tag(b)}</b></td>
        <td class="v">${f0(g[b + '.load'])} · ${f0(g[b + '.met'])}${g[b + '.met'] < g[b + '.load'] - 20 ? ' ' + pill('UNMET', 'err') : ''}</td>
        <td class="v">${f0(g[b + '.flow'])}</td><td class="v">${f0(g[b + '.valve'])}%</td>
        <td class="v">${f1(g[b + '_hw_remote_dp'])}</td><td class="v">${f1(g[b + '.rwt'])}</td>
        <td class="v">${f1(g[b + '_hw_eob_temp'])}</td><td class="v">${f0(g[b + '.eob_pos'])}% · ${f1(g[b + '.eob_flow'])} gpm</td>
        <td class="v">${f1(g[b + '.conn_flow'])} <span class="muted">${(L.connected || []).filter(c => c.branch === tag(b)).map(c => c.name).join(', ')}</span></td></tr>`).join('');

      const devView = d => vview(g, Object.keys(L.tags || {}).find(k => L.tags[k] === d.name) || '');
      $('hp_dev').innerHTML = (s.devices || []).map(d => `<tr><td><b>${d.name}</b></td><td>device ${d.device_instance} · ${d.address}</td>
        <td>${d.kind === 'boiler' ? (devView(d).integration || 'boiler BMS objects') : 'I/O for ALC ' + d.controller} · ${d.objects} objects</td>
        <td>${d.offline ? pill('OFFLINE', 'err') : pill('online', 'on')}</td></tr>`).join('') +
        (s.controllers || []).map(c => `<tr><td>ALC ${c.device_instance}</td><td colspan="2" class="muted">${c.status}</td>
        <td>${c.ok === c.points ? pill(c.ok + '/' + c.points + ' writes OK', 'on') : pill(c.ok + '/' + c.points + ' writes OK', 'err')}</td></tr>`).join('');
    },
  };

  window.hwPower = async function (state) {
    const m = document.getElementById('hp_minutes');
    await fetch('/api/power', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({state, minutes: m && m.value ? parseFloat(m.value) : null})});
    refresh();
  };
})();
