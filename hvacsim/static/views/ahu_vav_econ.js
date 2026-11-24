// Equipment view: VAV AHU with economizer, 4-fan supply array, return fan
(function () {
  const tcol = t => { const x = Math.max(0, Math.min(1, (t - 40) / 60)); const l = (a, b) => Math.round(a + (b - a) * x);
    return `rgb(${l(60, 220)},${l(120, 60)},${l(220, 40)})`; };
  const f1 = v => (v == null ? '—' : Number(v).toFixed(1));
  const f0 = v => (v == null ? '—' : Math.round(Number(v)).toLocaleString());
  const DR = ['sf1', 'sf2', 'sf3', 'sf4', 'rf'];
  const LBL = {sf1: 'SF-1', sf2: 'SF-2', sf3: 'SF-3', sf4: 'SF-4', rf: 'RF'};

  function damper(x, y, vertical, id) {
    return vertical
      ? `<g id="${id}"><line x1="${x}" y1="${y - 22}" x2="${x}" y2="${y + 22}" stroke="var(--line)" stroke-width="2"/>
         <line class="blade" x1="${x - 12}" y1="${y}" x2="${x + 12}" y2="${y}" stroke="var(--ink)" stroke-width="3"/></g>`
      : `<g id="${id}"><line x1="${x - 22}" y1="${y}" x2="${x + 22}" y2="${y}" stroke="var(--line)" stroke-width="2"/>
         <line class="blade" x1="${x}" y1="${y - 12}" x2="${x}" y2="${y + 12}" stroke="var(--ink)" stroke-width="3"/></g>`;
  }
  function coil(x, label, id) {
    return `<rect id="${id}" x="${x}" y="182" width="16" height="66" rx="2" fill="var(--chip)" stroke="var(--ink)"/>
      <text x="${x + 8}" y="176" text-anchor="middle" class="al">${label}</text>`;
  }
  function fan(x, y, i) {
    return `<g><circle cx="${x}" cy="${y}" r="15" fill="none" stroke="var(--line)" stroke-width="1.5"/>
      <g id="fan_${i}" class="ahufan"><path d="M${x},${y} l-4,-12 l8,0 z M${x},${y} l12,-4 l0,8 z M${x},${y} l4,12 l-8,0 z M${x},${y} l-12,4 l0,-8 z" fill="var(--muted)"/></g>
      <text x="${x + 20}" y="${y + 4}" class="al" id="fanlbl_${i}"></text></g>`;
  }

  VIEWS.ahu_vav_econ = {
    html: `
    <style>
      .ahusvg text.al { font: 10px system-ui, sans-serif; fill: var(--muted); }
      .ahusvg text.av { font: 600 11px system-ui, sans-serif; fill: var(--ink); }
      .ahufan { transform-box: fill-box; transform-origin: center; animation: spin .6s linear infinite; animation-play-state: paused; }
      .safety { display:flex; flex-wrap:wrap; gap:8px; margin: 8px 0; }
      .safety .pill { font-size: 13px; padding: 3px 10px; }
      .drv td, .drv th { padding: 4px 5px; font-size: 13px; }
      .drv button { padding: 2px 7px; font-size: 12px; }
      .drv button.sel { background: var(--accent); color: #fff; border-color: var(--accent); }
    </style>
    <svg class="ahusvg" viewBox="0 0 760 300" role="img" aria-label="Air handler schematic">
      <!-- return duct -->
      <rect x="70" y="40" width="660" height="50" fill="none" stroke="var(--ink)" stroke-width="1.5"/>
      <text x="735" y="70" class="al">RA</text><text x="40" y="70" class="al">EA</text>
      ${damper(95, 65, false, 'd_ea')}
      <text x="95" y="34" text-anchor="middle" class="al">EA</text><text x="95" y="108" text-anchor="middle" class="av" id="t_ea"></text>
      <g><circle cx="300" cy="65" r="17" fill="none" stroke="var(--line)" stroke-width="1.5"/>
         <g id="fan_rf" class="ahufan"><path d="M300,65 l-4,-13 l8,0 z M300,65 l13,-4 l0,8 z M300,65 l4,13 l-8,0 z M300,65 l-13,4 l0,-8 z" fill="var(--muted)"/></g></g>
      <text x="300" y="34" text-anchor="middle" class="al">RF</text><text x="325" y="62" class="av" id="t_rf"></text>
      <text x="520" y="58" class="al">RAT / RH / CO2</text><text x="520" y="74" class="av" id="t_ra"></text>
      <!-- RA damper (vertical connection) -->
      <rect x="140" y="90" width="40" height="92" fill="none" stroke="var(--ink)" stroke-width="1.5"/>
      ${damper(160, 136, true, 'd_ra')}
      <text x="188" y="140" class="al">RA</text><text x="188" y="154" class="av" id="t_radmpr"></text>
      <!-- supply path -->
      <rect x="20" y="182" width="720" height="66" fill="none" stroke="var(--ink)" stroke-width="1.5"/>
      <text x="0" y="220" class="al">OA</text>
      ${damper(60, 215, false, 'd_oa')}
      <text x="60" y="270" text-anchor="middle" class="av" id="t_oa"></text>
      <rect x="205" y="182" width="10" height="66" fill="url(#pf)" stroke="var(--ink)"/><text x="210" y="176" text-anchor="middle" class="al">PF</text>
      <text x="210" y="270" text-anchor="middle" class="av" id="t_pf"></text>
      ${coil(240, 'PH', 'c_ph')}
      <text x="248" y="270" text-anchor="middle" class="av" id="t_ph"></text>
      <text x="290" y="176" text-anchor="middle" class="al">MAT</text><text x="290" y="200" text-anchor="middle" class="av" id="t_mat"></text>
      <text x="290" y="230" text-anchor="middle" class="al">FZ</text><text x="290" y="244" text-anchor="middle" class="av" id="t_fz"></text>
      ${fan(345, 195, 1)}${fan(345, 235, 2)}${fan(410, 195, 3)}${fan(410, 235, 4)}
      ${coil(470, 'CHW', 'c_cc')}
      <text x="478" y="270" text-anchor="middle" class="av" id="t_cc"></text>
      ${coil(510, 'HW', 'c_hc')}
      <text x="518" y="270" text-anchor="middle" class="av" id="t_hc"></text>
      <line x1="555" y1="186" x2="555" y2="244" stroke="var(--accent)" stroke-width="2" stroke-dasharray="3 3" id="hum_grid"/>
      <text x="555" y="176" text-anchor="middle" class="al">HUM</text><text x="555" y="270" text-anchor="middle" class="av" id="t_hum"></text>
      <text x="610" y="176" text-anchor="middle" class="al">SAT / RH</text><text x="610" y="210" text-anchor="middle" class="av" id="t_sat"></text>
      <text x="610" y="226" text-anchor="middle" class="av" id="t_sarh"></text>
      <text x="690" y="176" text-anchor="middle" class="al">duct static</text><text x="690" y="210" text-anchor="middle" class="av" id="t_sp"></text>
      <text x="690" y="226" text-anchor="middle" class="al" id="t_dis"></text>
      <text x="745" y="220" class="al">SA</text>
      <text x="20" y="295" class="al" id="t_summary"></text>
      <defs><pattern id="pf" width="4" height="4" patternUnits="userSpaceOnUse"><path d="M0,4 l4,-4" stroke="var(--muted)"/></pattern></defs>
    </svg>
    <div class="safety" id="ahu_safety"></div>
    <table class="drv" style="margin-top:6px">
      <thead><tr><th>Drive</th><th>Keypad</th><th>Status</th><th class="v">Hz</th><th class="v">A</th><th>Hand ref</th><th>Warnings</th></tr></thead>
      <tbody id="ahu_drives"></tbody>
    </table>
    <table style="margin-top:10px"><tbody id="ahu_vals"></tbody></table>`,

    render(s) {
      const g = s.signals, $ = id => document.getElementById(id);
      const turn = (id, open, vertical) => { const b = document.querySelector('#' + id + ' .blade');
        if (b) b.setAttribute('transform', `rotate(${(1 - open / 100) * 90 * (vertical ? -1 : 1)} ${b.getAttribute('x1') * 0.5 + b.getAttribute('x2') * 0.5} ${b.getAttribute('y1') * 0.5 + b.getAttribute('y2') * 0.5})`); };
      turn('d_oa', g.oa_dmpr_pos); turn('d_ra', g.ra_dmpr_pos, true); turn('d_ea', g.ea_dmpr_pos);
      $('t_oa').textContent = `${f0(g.oa_dmpr_pos)}% · ${f0(g.oa_cfm)} cfm`;
      $('t_ea').textContent = `${f0(g.ea_dmpr_pos)}%`;
      $('t_radmpr').textContent = `${f0(g.ra_dmpr_pos)}%`;
      $('t_ra').textContent = `${f1(g.ra_temp)}°F ${f0(g.ra_rh)}% ${f0(g.ra_co2)} ppm`;
      $('t_rf').textContent = `${f1(g.rf_freq)} Hz · ${f0(g.return_cfm)} cfm`;
      $('t_pf').textContent = `${Number(g.pf_dp).toFixed(2)}"`;
      $('c_ph').setAttribute('fill', g.ph_frozen ? 'var(--bad)' : tcol(g.ph_lwt));
      $('c_cc').setAttribute('fill', g.cc_frozen ? 'var(--bad)' : tcol(g.cc_lwt));
      $('c_hc').setAttribute('fill', g.hc_frozen ? 'var(--bad)' : tcol(g.hc_lwt));
      $('t_ph').textContent = `${f0(g.ph_vlv_pos)}%`;
      $('t_cc').textContent = `${f0(g.clg_vlv_pos)}%`;
      $('t_hc').textContent = `${f0(g.htg_vlv_pos)}%`;
      $('t_hum').textContent = g.hum_hl_tripped ? 'HL TRIP' : `${f0(g.hum_vlv_pos)}%`;
      $('hum_grid').style.opacity = g.steam_lb_h > 0 ? 1 : 0.2;
      $('t_mat').textContent = `${f1(g.ma_temp)}°F`;
      $('t_fz').textContent = g.frz_tripped ? 'TRIPPED' : `${f1(g.coldest_temp)}°F`;
      $('t_sat').textContent = `${f1(g.sa_temp)}°F`;
      $('t_sarh').textContent = `${f0(g.sa_rh)}% RH`;
      $('t_sp').textContent = `${Number(g.sa_static).toFixed(2)}" w.c.`;
      $('t_dis').textContent = `discharge ${Number(g.discharge_static).toFixed(2)}"`;
      for (let i = 1; i <= 4; i++) {
        const hz = g['sf' + i + '_freq'], el = $('fan_' + i);
        el.style.animationPlayState = hz > 1 ? 'running' : 'paused';
        if (hz > 1) el.style.animationDuration = (20 / hz).toFixed(2) + 's';
        $('fanlbl_' + i).textContent = `${f0(g['sf' + i + '_cfm'])}`;
      }
      const rf = $('fan_rf'); rf.style.animationPlayState = g.rf_freq > 1 ? 'running' : 'paused';
      if (g.rf_freq > 1) rf.style.animationDuration = (20 / g.rf_freq).toFixed(2) + 's';
      $('t_summary').textContent = `${g.sim_clock} · ${g.occupied ? 'occupied' : 'unoccupied'} · supply ${f0(g.supply_cfm)} cfm (VAV demand ${f0(g.vav_demand_cfm)}${g.starved ? ', STARVED' : ''}) · OA ${f1(g.oa_fraction)}% · bldg ${Number(g.building_p).toFixed(3)}" · zone ${f1(g.zone_temp)}°F · OAT ${f1(g.oat)}°F`;

      const pill = (ok, txt) => `<span class="pill ${ok ? 'on' : 'err'}">${txt}</span>`;
      $('ahu_safety').innerHTML =
        pill(g.frz_stat, 'Freezestat') + ` <button onclick="keypad('frz_reset')">Reset freezestat</button>` +
        pill(g.hi_static, 'High static') + ` <button onclick="keypad('hs_reset')">Reset high static</button>` +
        pill(g.ra_smoke, 'RA smoke') + pill(g.sa_smoke, 'SA smoke') +
        pill(g.safety_chain_ok, g.safety_chain_ok ? 'Safety chain → DI4 closed' : 'Safety chain OPEN: drives coast') +
        (g.ph_frozen || g.cc_frozen || g.hc_frozen ? pill(false, 'COIL FROZEN') + ` <button onclick="keypad('coil_repair')">Repair coils</button>` : '') +
        (g.hum_hl_tripped ? pill(false, 'Hum high limit') : '') + (g.duct_wetting ? pill(false, 'Wet duct') : '');

      const rows = (g.drives || '').split('\n').filter(Boolean).map(line => {
        const [d, mode, status, hz, amps, warn, href] = line.split('|');
        const btn = k => `<button class="${mode === k ? 'sel' : ''}" onclick="keypad('${d}.${k}')">${k[0].toUpperCase() + k.slice(1)}</button>`;
        return `<tr><td><b>${LBL[d]}</b></td><td>${btn('off')} ${btn('hand')} ${btn('auto')} <button onclick="keypad('${d}.reset')">Reset</button></td>
          <td>${status}</td><td class="v">${hz}</td><td class="v">${amps}</td>
          <td><input type="number" value="${href}" style="width:56px" onchange="keypad('${d}.hand_ref', parseFloat(this.value))"></td>
          <td class="muted">${warn}</td></tr>`;
      });
      const tb = $('ahu_drives');
      if (!tb.contains(document.activeElement)) tb.innerHTML = rows.join('');

      const vals = [
        ['True mixed air (before preheat)', f1(g.mixed_temp) + ' °F'], ['Preheat leaving / coil water', f1(g.ph_lat) + ' / ' + f1(g.ph_lwt) + ' °F'],
        ['Fan leaving (fan heat)', f1(g.fan_lat) + ' °F'], ['CHW coil leaving air / water', f1(g.cc_lat) + ' / ' + f1(g.cc_lwt) + ' °F · ' + f1(g.cc_tons) + ' tons'],
        ['HW coil leaving air', f1(g.hc_lat) + ' °F · ' + f1(g.hc_mbh) + ' MBH'], ['Supply dew point', f1(g.sa_dewpoint) + ' °F'],
        ['Coil flows PH / CHW / HW', f1(g.ph_gpm) + ' / ' + f1(g.cc_gpm) + ' / ' + f1(g.hc_gpm) + ' gpm'],
        ['Fan total pressure', Number(g.fan_dp).toFixed(2) + '" w.c.'], ['Mixing / return plenum', Number(g.mix_plenum_p).toFixed(3) + ' / ' + Number(g.return_plenum_p).toFixed(3) + '"'],
        ['VAV boxes open (aggregate)', f0(g.vav_open_pct) + ' %'], ['Steam', f1(g.steam_lb_h) + ' lb/h'],
        ['Barometric dampers SF-1..4', [1, 2, 3, 4].map(i => f0(g['sf' + i + '_bd_pos']) + '%').join(' / ')],
      ];
      $('ahu_vals').innerHTML = vals.map(([k, v]) => `<tr><td>${k}</td><td class="v">${v}</td></tr>`).join('');
    },
  };
})();
