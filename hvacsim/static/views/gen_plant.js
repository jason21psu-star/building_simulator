// Equipment view: natural gas generator plant (gensets, paralleling bus, ATSs, Modbus TCP)
(function () {
  const f0 = v => (v == null || isNaN(v) ? '—' : Math.round(Number(v)).toLocaleString());
  const f1 = v => (v == null || isNaN(v) ? '—' : Number(v).toFixed(1));
  const pill = (txt, cls) => `<span class="pill ${cls || ''}">${txt}</span>`;
  const btn = (action, label) => `<button onclick="keypad('${action}')">${label}</button>`;
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
  const POS = {N: 'NORMAL', E: 'EMERGENCY', O: 'OPEN', B: 'BOTH'};

  VIEWS.gen_plant = {
    wide: true,
    html: `
    <style>
      .gp h3 { font-size: 13px; margin: 14px 0 6px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
      .gp table td, .gp table th { padding: 4px 5px; font-size: 13px; }
      .gp .kpis { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
      .gp .kpi { background: var(--chip); border-radius: 8px; padding: 8px 10px; }
      .gp .kpi b { display: block; font-size: 18px; font-variant-numeric: tabular-nums; }
      .gp .kpi span { color: var(--muted); font-size: 12px; }
      .gp .btns button { padding: 2px 7px; font-size: 12px; }
      .gp .scroll { overflow-x: auto; }
      .gp .pill { white-space: nowrap; }
      .gp .power { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 10px; }
      .gp .oneline { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 8px; }
      .gp .ats { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; }
      .gp .ats .src { display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); }
      .gp .ats .src b.live { color: var(--ok); }
      .gp .ats .sw { font-size: 16px; font-weight: 600; margin: 4px 0; }
      .gp svg.trend { width: 100%; height: 70px; background: var(--chip); border-radius: 6px; }
    </style>
    <div class="gp">
      <div class="power" id="gp_power"></div>
      <div class="kpis" id="gp_kpis"></div>
      <h3>One-line: utility · generator bus · ATSs</h3><div class="oneline" id="gp_ats"></div>
      <h3>Bus frequency (last 5 min)</h3><div id="gp_trend"></div>
      <h3 id="gp_gens_h">Generators</h3><div class="scroll"><table><thead><tr>
        <th>Set</th><th>Unit ID</th><th>Status</th><th>Switch</th><th>kW · %</th><th>Hz · V · A</th><th>Coolant °F</th>
        <th>Oil psi</th><th>Battery V</th><th>Hours · starts</th><th>Fault</th><th>Control</th></tr></thead><tbody id="gp_gens"></tbody></table></div>
      <h3>Modbus TCP server</h3><div id="gp_mb"></div>
      <h3>Hardwired I/O (ALC)</h3><table><tbody id="gp_dev"></tbody></table>
    </div>`,

    render(s) {
      const g = s.signals, $ = id => document.getElementById(id);
      let L; try { L = JSON.parse(g.layout || '{}'); } catch (e) { L = {}; }
      const tag = p => (L.tags || {})[p] || p.toUpperCase();
      const h = s.host || {};
      $('gp_power').innerHTML = `Utility power ${g.utility_ok ? pill('NORMAL', 'on') : pill('FAILED', 'err')}
        ${h.restore_in != null ? `<span class="muted">restores in ${Math.ceil(h.restore_in / 60)} min</span>` : ''}
        <button onclick="genPower('fail')">Fail utility</button>
        <label>for <input type="number" id="gp_minutes" min="0" step="1" style="width:70px" placeholder="min"></label>
        <button onclick="genPower('restore')">Restore</button>
        <span class="muted">${g.sim_clock}</span>`;
      const kpi = (v, label) => `<div class="kpi"><b>${v}</b><span>${label}</span></div>`;
      $('gp_kpis').innerHTML = [
        kpi(f0(g.site_kw) + ' kW', 'site load'), kpi(f0(g.bus_kw) + ' / ' + f0(g.online_kw), 'bus kW / online capacity'),
        kpi(f0(g.sets_online), 'sets on the bus'), kpi(f1(g.bus_freq) + ' Hz', 'bus frequency'),
        kpi(f0(g.bus_volts) + ' V', 'bus volts'), kpi(f0(g.building_kw) + ' kW', 'building (non-HVAC)'),
        kpi(g.shed_level > 0 ? f0(g.shed_level) : 'none', 'load shed level')].join('');

      const busLive = g.sets_online > 0 && g.bus_freq > 50;
      $('gp_ats').innerHTML = (L.ats || []).map(a => {
        const pos = g[a + '.pos'] || 'N';
        const cls = pos === 'E' ? 'warn' : pos === 'O' ? 'err' : 'on';
        return `<div class="ats"><b>${tag(a)}</b> <span class="muted">${(L.ats_desc || {})[a] || ''}</span>
          <div class="src"><span>N: <b class="${g.utility_ok ? 'live' : ''}">${g.utility_ok ? 'utility live' : 'utility dead'}</b></span>
            <span>E: <b class="${busLive ? 'live' : ''}">${busLive ? 'bus live' : 'bus dead'}</b></span></div>
          <div class="sw">${pill(POS[pos], cls)} ${g[a + '.live'] ? pill('load ON', 'on') : pill('load DARK', 'err')}</div>
          <div class="muted" style="font-size:12px">${g[a + '.status'] || ''}</div>
          <div class="v">${f0(g[a + '.kw'])} kW · ${f0(g[a + '.amps'])} A</div>
          <div class="muted" style="font-size:12px">${((L.feeds || {})[a] || []).join(', ')}</div></div>`;
      }).join('');

      const tr = (g.freq_trend || '').split(',').filter(x => x !== '').map(Number);
      if (tr.length > 1) {
        const lo = 54, hi = 61, W = 600, H = 70;
        const y = v => H - (Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo) * H;
        const pts = tr.map((v, i) => `${(i / (tr.length - 1) * W).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
        $('gp_trend').innerHTML = `<svg class="trend" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
          <line x1="0" x2="${W}" y1="${y(60)}" y2="${y(60)}" stroke="currentColor" stroke-opacity=".25" stroke-dasharray="4 4"/>
          <line x1="0" x2="${W}" y1="${y(55)}" y2="${y(55)}" stroke="#c0392b" stroke-opacity=".5" stroke-dasharray="4 4"/>
          <polyline points="${pts}" fill="none" stroke="#2f7de1" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>
          <span class="muted">dashed: 60 Hz and the 55 Hz underfrequency trip level · ${tr.some(v => v > 0) ? 'lowest ' + f1(Math.min(...tr.filter(v => v > 0))) + ' Hz' : 'bus not energized in this window'}</span>`;
      } else {
        $('gp_trend').innerHTML = '<span class="muted">bus not energized</span>';
      }

      const gv = (L.generators || []).map(p => vview(g, p));
      $('gp_gens_h').textContent = vheading(gv, 'Generators');
      $('gp_gens').innerHTML = (L.generators || []).map((p, i) => {
        const v = gv[i];
        const fc = g[p + '.fault_code'];
        return `<tr><td><b>${tag(p)}</b><br><span class="muted">${v.short || ''}</span></td><td class="v">${(L.units || {})[p]}</td>
          <td>${vstatus(v)}</td>
          <td>${g[p + '.switch']}</td><td class="v">${f0(g[p + '.kw'])} · ${f0(g[p + '.pct'])}%</td>
          <td class="v">${f1(g[p + '.freq'])} · ${f0(g[p + '.volts'])} · ${f0(g[p + '.amps'])}</td>
          <td class="v">${f0(g[p + '.coolant'])}</td><td class="v">${f0(g[p + '.oil'])}</td><td class="v">${f1(g[p + '.battery'])}</td>
          <td class="v">${f1(g[p + '.hours'])} · ${f0(g[p + '.starts'])}</td><td>${fc ? pill(f0(fc), v.state === 'lockout' ? 'err' : 'warn') : ''}</td>
          <td class="btns">${vpanel(p, v)}</td></tr>`;
      }).join('');

      const mb = s.modbus;
      $('gp_mb').innerHTML = mb ? `<div>${pill(mb.status, mb.status.startsWith('listening') ? 'on' : 'err')}
          <span class="muted">unit IDs ${mb.units.join(', ')} (master ${L.master_unit}) · ${mb.requests} requests · ${mb.errors} errors · ${mb.clients} client(s)</span></div>
        <table><thead><tr><th>Time</th><th>Client</th><th>Unit</th><th>FC</th><th>Register</th><th>Count</th><th>Result</th></tr></thead><tbody>
        ${mb.log.map(r => `<tr><td>${r.t}</td><td>${r.client}</td><td class="v">${r.unit}</td><td class="v">${r.fc}</td>
          <td class="v">${r.start ?? ''}</td><td class="v">${r.count}</td><td>${r.result === 'ok' ? pill('ok', 'on') : pill(r.result, 'err')}</td></tr>`).join('')}
        </tbody></table>` : '<span class="muted">Modbus server not running</span>';

      $('gp_dev').innerHTML = (s.devices || []).map(d => `<tr><td><b>${d.name}</b></td><td>device ${d.device_instance} · ${d.address}</td>
        <td>I/O for ALC ${d.controller} · ${d.objects} objects</td><td>${d.offline ? pill('OFFLINE', 'err') : pill('online', 'on')}</td></tr>`).join('') +
        (s.controllers || []).map(c => `<tr><td>ALC ${c.device_instance}</td><td colspan="2" class="muted">${c.status}</td>
        <td>${c.ok === c.points ? pill(c.ok + '/' + c.points + ' writes OK', 'on') : pill(c.ok + '/' + c.points + ' writes OK', 'err')}</td></tr>`).join('');
    },
  };

  window.genPower = async function (state) {
    const m = document.getElementById('gp_minutes');
    await fetch('/api/power', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({state, minutes: m && m.value ? parseFloat(m.value) : null})});
    refresh();
  };
})();
