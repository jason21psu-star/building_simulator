// Equipment view: chilled water plant (chillers, pumps, towers, TES, risers)
(function () {
  const f0 = v => (v == null || isNaN(v) ? '—' : Math.round(Number(v)).toLocaleString());
  const f1 = v => (v == null || isNaN(v) ? '—' : Number(v).toFixed(1));
  const tcol = t => { const x = Math.max(0, Math.min(1, (t - 42) / 16)); const l = (a, b) => Math.round(a + (b - a) * x);
    return `rgb(${l(40, 225)},${l(110, 80)},${l(230, 50)})`; };
  const pill = (txt, cls) => `<span class="pill ${cls || ''}">${txt}</span>`;
  const btn = (action, label, value) => `<button onclick="keypad('${action}'${value !== undefined ? ',' + JSON.stringify(value) : ''})">${label}</button>`;
  const vpos = (g, base) => { const p = g[base + '.pos']; return `<span title="${base}">${p >= 98 ? '▮' : p <= 2 ? '▯' : '◧'} ${f0(p)}%</span>`; };

  VIEWS.chw_plant = {
    wide: true,          // full-width page: the plant tables need the room
    html: `
    <style>
      .cp h3 { font-size: 13px; margin: 14px 0 6px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
      .cp table td, .cp table th { padding: 4px 5px; font-size: 13px; }
      .cp .kpis { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
      .cp .kpi { background: var(--chip); border-radius: 8px; padding: 8px 10px; }
      .cp .kpi b { display: block; font-size: 18px; font-variant-numeric: tabular-nums; }
      .cp .kpi span { color: var(--muted); font-size: 12px; }
      .cp .btns button { padding: 2px 7px; font-size: 12px; }
      .cp .scroll { overflow-x: auto; }
      .cp .pill { white-space: nowrap; }
      .cp td.st { max-width: 340px; }
      .cp .power { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 10px; }
      .cp .tank { display: inline-flex; flex-direction: column; width: 26px; height: 90px; border: 1px solid var(--line); border-radius: 4px; overflow: hidden; vertical-align: middle; }
      .cp .tank div { flex: 1; }
    </style>
    <div class="cp">
      <div class="power" id="cp_power"></div>
      <div class="kpis" id="cp_kpis"></div>
      <h3>Chillers (YORK YK, BACnet)</h3><div class="scroll"><table><thead><tr>
        <th>Chiller</th><th>Panel status</th><th>LCHWT / ECHWT</th><th>SP</th><th>Tons</th><th>%FLA · kW</th><th>kW/t</th>
        <th>Evap · Cond gpm</th><th>Lift</th><th>CHW · CW valve</th><th>Panel</th></tr></thead><tbody id="cp_ch"></tbody></table></div>
      <h3>Pumps (ACH580, hardwired)</h3><div class="scroll"><table><thead><tr>
        <th>Pump</th><th>Drive</th><th>Hz</th><th>gpm</th><th>Head ft</th><th>Amps</th><th>Keypad</th></tr></thead><tbody id="cp_pumps"></tbody></table></div>
      <h3>Cooling towers</h3><div class="scroll"><table><thead><tr>
        <th>Cell</th><th>Fan</th><th>Fill · bypass gpm</th><th>Leaving · basin °F</th><th>Level in</th><th>Inlet · outlet · bypass · eq</th><th>Heater</th><th></th></tr></thead><tbody id="cp_ct"></tbody></table></div>
      <h3>Thermal storage (decoupler)</h3><div class="scroll"><table><thead><tr>
        <th>Tank</th><th>Profile</th><th>Charge</th><th>Ton-hr</th><th>Flow gpm</th><th>Top · bottom valve</th></tr></thead><tbody id="cp_tk"></tbody></table></div>
      <h3>Building risers</h3><div class="scroll"><table><thead><tr>
        <th>Riser</th><th>Load · met tons</th><th>Flow gpm</th><th>Coil valves</th><th>Remote DP psi</th><th>Return °F</th><th>AHU gpm</th></tr></thead><tbody id="cp_br"></tbody></table></div>
      <h3>BACnet devices</h3><table><tbody id="cp_dev"></tbody></table>
    </div>`,

    render(s) {
      const g = s.signals, $ = id => document.getElementById(id);
      let L; try { L = JSON.parse(g.layout || '{}'); } catch (e) { L = {}; }
      const tag = p => (L.tags || {})[p] || p.toUpperCase();
      const h = s.host || {};
      $('cp_power').innerHTML = `Utility power ${g.utility_ok ? pill('NORMAL', 'on') : pill('FAILED', 'err')}
        ${h.restore_in != null ? `<span class="muted">restores in ${Math.ceil(h.restore_in / 60)} min</span>` : ''}
        <button onclick="plantPower('fail')">Fail utility</button>
        <label>for <input type="number" id="cp_minutes" min="0" step="1" style="width:70px" placeholder="min"></label>
        <button onclick="plantPower('restore')">Restore</button>
        <span class="muted">${g.sim_clock} · ${g.occupied ? 'occupied' : 'unoccupied'} · OAT ${f1(g.oat)}°F · WB ${f1(g.oa_wb)}°F</span>`;
      const kpi = (v, label) => `<div class="kpi"><b>${v}</b><span>${label}</span></div>`;
      $('cp_kpis').innerHTML = [
        kpi(f0(g.building_tons) + ' t', 'building load'), kpi(f0(g.plant_tons) + ' t', 'chillers'),
        kpi(f0(g.plant_kw) + ' kW', 'plant power'),
        kpi(f0(g.primary_flow), 'primary gpm'), kpi(f0(g.secondary_flow), 'secondary gpm'),
        kpi((g.tes_flow > 0 ? '+' : '') + f0(g.tes_flow), g.tes_flow >= 0 ? 'decoupler gpm (charging)' : 'decoupler gpm (discharging)'),
        kpi(f1(g.tes_charge) + ' %', `TES charge · ${f0(g.tes_ton_hours)} ton-hr`),
        kpi(f1(g.pchws_actual) + ' / ' + f1(g.schws_actual) + '°F', 'primary / secondary supply'),
        kpi(f1(g.schwr_temp) + '°F', 'secondary return'), kpi(f1(g.plant_dp) + ' psi', 'plant DP'),
        kpi(f0(g.cw_flow) + ' gpm', `CW · supply ${f1(g.cws_actual)}°F`)].join('');

      $('cp_ch').innerHTML = (L.chillers || []).map(c => {
        const st = g[c + '.status'] || '';
        const cls = /SAFETY/.test(st) ? 'err' : /Running/.test(st) ? 'on' : '';
        return `<tr><td><b>${tag(c)}</b><br><span class="muted">${g[c + '.control_source'] ? 'Remote (ISN)' : 'LOCAL'} · switch ${g[c + '.panel_switch_run'] ? 'RUN' : 'STOP'}</span></td>
          <td class="st">${pill(st.split(' · ')[0], cls)}<div class="muted" style="font-size:12px">${st.split(' · ').slice(1).join(' · ')}</div>
            ${g[c + '.anti_recycle'] > 0 ? `<span class="muted">anti-recycle ${f0(g[c + '.anti_recycle'])} min</span>` : ''}</td>
          <td class="v">${f1(g[c + '.lchwt'])} / ${f1(g[c + '.echwt'])}</td><td class="v">${f1(g[c + '.active_sp'])}</td>
          <td class="v">${f0(g[c + '.tons'])}</td><td class="v">${f0(g[c + '.pct_fla'])}% · ${f0(g[c + '.kw'])}</td>
          <td class="v">${g[c + '.kw_per_ton'] ? Number(g[c + '.kw_per_ton']).toFixed(2) : '—'}</td>
          <td class="v">${f0(g[c + '_chw_flow'])} · ${f0(g[c + '_cw_flow'])}</td>
          <td class="v">${f1(g[c + '.cond_sat'] - g[c + '.evap_sat'])}°F<br><span class="muted">${f0(g[c + '.evap_p'])}/${f0(g[c + '.cond_p'])} psig</span></td>
          <td>${vpos(g, c + '_chw_vlv')} · ${vpos(g, c + '_cw_vlv')}</td>
          <td class="btns">${btn(c + '.remote', 'Remote')}${btn(c + '.local', 'Local')}${btn(c + '.run', 'Run')}${btn(c + '.stop', 'Stop')}${btn(c + '.reset', 'Reset')}</td></tr>`;
      }).join('');

      const pumpRow = p => {
        const st = (g[p + '.status'] || '').split('|');
        const cls = /FAULT|NO POWER/.test(st[1]) ? 'err' : /RUNNING/.test(st[1]) ? 'on' : '';
        return `<tr><td><b>${tag(p)}</b></td><td>${pill(st[1] || '', cls)} <span class="muted">${(st[0] || '').toUpperCase()}${st[4] ? ' · ' + st[4] : ''}</span></td>
          <td class="v">${f1(g[p + '.freq'])}</td><td class="v">${f0(g[p + '.flow'])}</td><td class="v">${f1(g[p + '.head'])}</td>
          <td class="v">${f1(g[p + '_amps'])}</td>
          <td class="btns">${btn(p + '.hand', 'Hand')}${btn(p + '.off', 'Off')}${btn(p + '.auto', 'Auto')}${btn(p + '.reset', 'Reset')}</td></tr>`;
      };
      $('cp_pumps').innerHTML = [...(L.primary_pumps || []), ...(L.secondary_pumps || []), ...(L.condenser_pumps || [])].map(pumpRow).join('');

      $('cp_ct').innerHTML = (L.towers || []).map(t => {
        const st = (g[t + '.status'] || '').split('|');
        const cls = /FAULT|NO POWER/.test(st[1]) ? 'err' : /RUNNING/.test(st[1]) ? 'on' : '';
        return `<tr><td><b>${tag(t)}</b>${g[t + '.vib_tripped'] ? '<br>' + pill('VIBRATION', 'err') : ''}${g[t + '.frozen'] ? '<br>' + pill('FROZEN', 'err') : ''}</td>
          <td>${pill(st[1] || '', cls)} ${f1(g[t + '.freq'])} Hz · ${f1(g[t + '_fan_amps'])} A${g[t + '.ice'] > 0 ? ` · ice ${f0(g[t + '.ice'])}%` : ''}</td>
          <td class="v">${f0(g[t + '.fill_flow'])} · ${f0(g[t + '.bypass_flow'])}</td>
          <td class="v">${f1(g[t + '.leaving'])} · ${f1(g[t + '_basin_temp'])}</td>
          <td class="v">${f1(g[t + '.level'])} ${g[t + '_level_lo'] ? '' : pill('LOW', 'err')}${g[t + '_level_hi'] ? '' : pill('HIGH', 'err')}</td>
          <td>${['inlet', 'outlet', 'bypass', 'eq'].map(v => vpos(g, `${t}_${v}_vlv`)).join(' · ')}</td>
          <td>${g[t + '.heater_on'] ? pill('ON', 'on') : pill('off')}</td>
          <td class="btns">${btn(t + '.vib_reset', 'Vib reset')}${btn(t + '.reset', 'Drive reset')}</td></tr>`;
      }).join('');

      $('cp_tk').innerHTML = (L.tanks || []).map(k => {
        const prof = (g[k + '.profile'] || '').split(',').map(Number);
        const bands = prof.filter((_, i) => i % 2 === 0).map(t => `<div style="background:${tcol(t)}" title="${t.toFixed(1)}°F"></div>`).join('');
        return `<tr><td><b>${tag(k)}</b></td><td><span class="tank">${bands}</span>
          <span class="muted">${f1(g[k + '_temp_01'])}° top · ${f1(g[k + '_temp_10'] ?? prof[prof.length - 1])}° bottom</span></td>
          <td class="v">${f1(g[k + '.charge'])}%</td><td class="v">${f0(g[k + '.ton_hours'])}</td>
          <td class="v">${(g[k + '.flow'] > 0 ? '+' : '') + f0(g[k + '.flow'])}</td>
          <td>${vpos(g, k + '_top_vlv')} · ${vpos(g, k + '_bot_vlv')}</td></tr>`;
      }).join('');

      $('cp_br').innerHTML = (L.branches || []).map(b => `<tr><td><b>${tag(b)}</b></td>
        <td class="v">${f0(g[b + '.load'])} · ${f0(g[b + '.met'])}${g[b + '.met'] < g[b + '.load'] - 5 ? ' ' + pill('UNMET', 'err') : ''}</td>
        <td class="v">${f0(g[b + '.flow'])}</td><td class="v">${f0(g[b + '.valve'])}%</td>
        <td class="v">${f1(g[b + '_chw_remote_dp'])}</td><td class="v">${f1(g[b + '.rwt'])}</td><td class="v">${f0(g[b + '.ahu_flow'])}</td></tr>`).join('');

      $('cp_dev').innerHTML = (s.devices || []).map(d => `<tr><td><b>${d.name}</b></td><td>device ${d.device_instance} · ${d.address}</td>
        <td>${d.kind === 'chiller' ? 'YORK OptiView BAS objects' : 'I/O for ALC ' + d.controller} · ${d.objects} objects</td>
        <td>${d.offline ? pill('OFFLINE', 'err') : pill('online', 'on')}</td></tr>`).join('') +
        (s.controllers || []).map(c => `<tr><td>ALC ${c.device_instance}</td><td colspan="2" class="muted">${c.status}</td>
        <td>${c.ok === c.points ? pill(c.ok + '/' + c.points + ' writes OK', 'on') : pill(c.ok + '/' + c.points + ' writes OK', 'err')}</td></tr>`).join('');
    },
  };

  window.plantPower = async function (state) {
    const m = document.getElementById('cp_minutes');
    await fetch('/api/power', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({state, minutes: m && m.value ? parseFloat(m.value) : null})});
    refresh();
  };
})();
