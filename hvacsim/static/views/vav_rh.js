// Equipment view: single-duct VAV box with hot water reheat and its zone
(function () {
  const tcol = t => { const x = Math.max(0, Math.min(1, (t - 50) / 50)); const l = (a, b) => Math.round(a + (b - a) * x);
    return `rgb(${l(60, 220)},${l(120, 60)},${l(220, 40)})`; };
  const f1 = v => (v == null ? '—' : Number(v).toFixed(1));
  const f0 = v => (v == null ? '—' : Math.round(Number(v)).toLocaleString());

  VIEWS.vav_rh = {
    html: `
    <style>
      .vavsvg text.al { font: 10px system-ui, sans-serif; fill: var(--muted); }
      .vavsvg text.av { font: 600 12px system-ui, sans-serif; fill: var(--ink); }
      .zs { display:flex; flex-wrap:wrap; gap:12px; align-items:center; margin-top:10px; padding:10px 12px;
            border:1px solid var(--line); border-radius:10px; background: var(--chip); }
      .zs .big { font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; }
      .zs input[type=range] { width: 170px; }
    </style>
    <svg class="vavsvg" viewBox="0 0 700 230" role="img" aria-label="VAV box and zone">
      <text x="10" y="16" class="al">from AHU</text><text x="10" y="32" class="av" id="v_inlet"></text>
      <rect x="10" y="70" width="330" height="44" fill="none" stroke="var(--ink)" stroke-width="1.5"/>
      <line x1="80" y1="70" x2="80" y2="114" stroke="var(--accent)" stroke-width="2"/><text x="80" y="64" text-anchor="middle" class="al">flow cross</text>
      <text x="80" y="132" text-anchor="middle" class="av" id="v_flow"></text>
      <circle cx="160" cy="92" r="18" fill="none" stroke="var(--line)"/>
      <line id="v_blade" x1="160" y1="74" x2="160" y2="110" stroke="var(--ink)" stroke-width="4"/>
      <text x="160" y="64" text-anchor="middle" class="al">damper</text><text x="160" y="132" text-anchor="middle" class="av" id="v_dmpr"></text>
      <rect id="v_coil" x="230" y="72" width="16" height="40" rx="2" fill="var(--chip)" stroke="var(--ink)"/>
      <text x="238" y="64" text-anchor="middle" class="al">reheat</text><text x="238" y="132" text-anchor="middle" class="av" id="v_rh"></text>
      <text x="300" y="64" text-anchor="middle" class="al">DAT</text><text x="300" y="96" text-anchor="middle" class="av" id="v_dat"></text>
      <path d="M340,92 L380,92" stroke="var(--ink)" stroke-width="1.5"/>
      <rect id="v_room" x="380" y="20" width="310" height="190" rx="8" fill="var(--chip)" stroke="var(--ink)" stroke-width="1.5"/>
      <text x="395" y="42" class="al" id="v_zone_lbl"></text>
      <text x="535" y="100" text-anchor="middle" style="font: 600 30px system-ui; fill: var(--ink)" id="v_zt"></text>
      <text x="535" y="124" text-anchor="middle" class="al" id="v_zt2"></text>
      <text x="395" y="170" class="av" id="v_occ"></text>
      <text x="395" y="190" class="al" id="v_load"></text>
      <text x="10" y="170" class="al" id="v_clock"></text>
      <text x="10" y="190" class="al" id="v_valve"></text>
    </svg>
    <div class="zs">
      <div><div class="muted">Zone sensor</div><div class="big" id="zs_t"></div></div>
      <label>Setpoint adjust <input type="range" id="zs_adj" step="0.5"> <b id="zs_adj_v"></b></label>
      <button onclick="keypad('override_push')">Press override (+30 min)</button>
      <span class="muted" id="zs_ovr"></span>
    </div>
    <table style="margin-top:10px"><tbody id="vav_vals"></tbody></table>`,

    render(s) {
      const g = s.signals, p = s.params, $ = id => document.getElementById(id);
      $('v_inlet').textContent = `${Number(g.inlet_static).toFixed(2)}" w.c. · ${f1(g.supply_temp_in)}°F`;
      $('v_flow').textContent = `${f0(g.airflow_1)} cfm`;
      const open = g.damper_pos / 100;
      $('v_blade').setAttribute('transform', `rotate(${open * 90} 160 92)`);
      $('v_dmpr').textContent = `${f0(g.damper_pos)}%`;
      $('v_coil').setAttribute('fill', g.reheat_mbh > 0.5 ? tcol(g.da_temp_actual) : 'var(--chip)');
      $('v_rh').textContent = `${f0(g.valve_pos)}%`;
      $('v_dat').textContent = `${f1(g.da_temp_1)}°F`;
      $('v_room').setAttribute('stroke', g.zone_temp_actual > 77 ? 'var(--bad)' : g.zone_temp_actual < 68 ? 'var(--accent)' : 'var(--ink)');
      $('v_zone_lbl').textContent = `${p.zone_type} zone · ${f0(p.area_sqft)} ft² · box ${f0(p.box_max_cfm)} cfm max`;
      $('v_zt').textContent = `${f1(g.zone_temp_actual)}°F`;
      $('v_zt2').textContent = `${f0(g.zone_rh_actual)}% RH · ${f0(g.zone_co2)} ppm CO2`;
      $('v_occ').textContent = `${g.occupied ? 'Occupied' : 'Unoccupied'} · ${f0(g.occupants)} people` +
        (g.override_remaining > 0 ? ` · override ${f0(g.override_remaining)} min left` : '');
      $('v_load').textContent = `room load ${f0(g.zone_load)} Btu/h · OAT ${f1(g.oat)}°F`;
      $('v_clock').textContent = g.sim_clock + (s.unit && s.host ? '' : '');
      $('v_valve').textContent = `reheat ${f1(g.reheat_mbh)} MBH · ${f1(g.reheat_gpm)} gpm`;
      $('zs_t').textContent = `${f1(g.zone_temp_1)}°`;
      const adj = $('zs_adj'), rng = Number(p.stpt_adj_range);
      adj.min = -rng; adj.max = rng;
      if (document.activeElement !== adj) adj.value = g.stpt_adj_1;
      adj.onchange = () => keypad('stpt_adj', parseFloat(adj.value));
      $('zs_adj_v').textContent = `${g.stpt_adj_1 > 0 ? '+' : ''}${f1(g.stpt_adj_1)}°F (±${rng})`;
      $('zs_ovr').textContent = g.override_1 ? 'button pressed' : (g.override_remaining > 0 ? `override timer ${f0(g.override_remaining)} min (max 120)` : '');
      const rows = [
        ['Airflow sensor / true airflow', f0(g.airflow_1) + ' / ' + f0(g.airflow_actual) + ' cfm'],
        ['Discharge air sensor / true', f1(g.da_temp_1) + ' / ' + f1(g.da_temp_actual) + ' °F'],
        ['Zone sensor / true zone temp', f1(g.zone_temp_1) + ' / ' + f1(g.zone_temp_actual) + ' °F'],
        ['Zone humidity sensor', f1(g.zone_humid_1) + ' %'],
        ['Inlet static', Number(g.inlet_static).toFixed(3) + '" w.c.'],
        ['Served by', s.served_by || 'standalone (Conditions panel)'],
      ];
      $('vav_vals').innerHTML = rows.map(([k, v]) => `<tr><td>${k}</td><td class="v">${v}</td></tr>`).join('');
    },
  };
})();
