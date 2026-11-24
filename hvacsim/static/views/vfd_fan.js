// Equipment view: ACH580-style VFD with direct-drive fan
(function () {
  const FAULTS = {2310: 'Overcurrent', 2330: 'Earth leakage', 3130: 'Input phase loss', 3210: 'DC link overvoltage',
    3220: 'DC link undervoltage', 4310: 'Excess temperature', 5091: 'Safe torque off', 6681: 'EFB comm loss',
    7121: 'Motor stall', 9081: 'External fault 1'};
  const fault = c => c ? `${c} ${FAULTS[c] || ''}` : '—';

  function diFunctions(p, i) {
    const d = 'di' + i, out = [];
    if (i === 1 && String(p.ext1_commands).startsWith('in1')) out.push('Start (20.01)');
    if (i === 2 && p.ext1_commands === 'in1_start_in2_dir') out.push('Direction (20.01)');
    if (p.run_permissive === d) out.push('Run permissive (20.40)');
    if (p.run_permissive === '-' + d) out.push('Run permissive, inverted (20.40)');
    if (p.start_interlock_1 === d) out.push('Start interlock 1 (20.41, stop: ' + p.start_interlock_stop_mode + ')');
    if (p.freq_ramp_in_zero === d) out.push('Ramp in zero (28.76): open = hold 0 Hz');
    if (p.external_event_1 === d) out.push('External event 1 (31.01, ' + p.external_event_1_type + ')');
    if (p.override_enable && p.override_source === d) out.push('Override (70.03)');
    if (Number(p.safety_di) === i) out.push('safety circuit wired here');
    return out.join(', ') || '<span class="muted">not used</span>';
  }
  const contact = v => v ? '<span class="pill on">CLOSED</span>' : '<span class="pill">OPEN</span>';

  VIEWS.vfd_fan = {
    html: `
    <style>
      .kp { display:grid; grid-template-columns: minmax(0,1fr) minmax(0,1fr); gap:14px; }
      @media (max-width: 700px) { .kp { grid-template-columns: 1fr; } }
      .lcd { background:#0f1a14; color:#c8f7d4; border-radius:10px; padding:12px 14px; font:13px ui-monospace,Menlo,Consolas,monospace; }
      .lcd .top { display:flex; justify-content:space-between; border-bottom:1px solid #2b4434; padding-bottom:6px; margin-bottom:8px; }
      .lcd .big { font-size:26px; font-weight:600; }
      .lcd .row3 { display:grid; grid-template-columns: repeat(3, 1fr); gap:6px; text-align:center; }
      .lcd .lbl { color:#7fb293; font-size:11px; }
      .lcd .flt { color:#ff8a80; font-weight:600; min-height:18px; margin-top:8px; }
      .lcd .wrn { color:#ffd27a; min-height:16px; font-size:12px; }
      .keys { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
      .keys button.sel { background: var(--accent); color:#fff; border-color: var(--accent); }
      .vfdsvg text { font: 11px system-ui, sans-serif; fill: var(--muted); }
      .vfdsvg .val { font: 600 13px system-ui, sans-serif; fill: var(--ink); }
      #vfd_fan { transform-box: fill-box; transform-origin: center; animation: spin 1s linear infinite; animation-play-state: paused; }
    </style>
    <div class="kp">
      <div>
        <div class="lcd" aria-live="polite">
          <div class="top"><span id="kp_mode">AUTO</span><span id="kp_dir"></span><span id="kp_state"></span></div>
          <div class="row3">
            <div><div class="big" id="kp_hz">0.0</div><div class="lbl">Output frequency Hz</div></div>
            <div><div class="big" id="kp_a">0.0</div><div class="lbl">Motor current A</div></div>
            <div><div class="big" id="kp_kw">0.0</div><div class="lbl">Output power kW</div></div>
          </div>
          <div style="margin-top:8px">Ref <span id="kp_ref"></span> Hz · DC bus <span id="kp_bus"></span> V · Drive <span id="kp_temp"></span> °C</div>
          <div class="flt" id="kp_fault"></div>
          <div class="wrn" id="kp_warn"></div>
        </div>
        <div class="keys">
          <button id="k_off" onclick="keypad('off')">Off</button>
          <button id="k_hand" onclick="keypad('hand')">Hand</button>
          <button id="k_auto" onclick="keypad('auto')">Auto</button>
          <button onclick="keypad('reset')">Reset</button>
          <label>Hand ref <input type="number" id="k_ref" step="1" min="0" max="60" style="width:70px"> Hz</label>
          <button onclick="keypad('hand_ref', parseFloat(document.getElementById('k_ref').value))">Set</button>
        </div>
      </div>
      <svg class="vfdsvg" viewBox="0 0 330 200" role="img" aria-label="Drive, motor and fan">
        <rect x="8" y="30" width="70" height="110" rx="6" fill="var(--chip)" stroke="var(--ink)" stroke-width="1.5" id="vfd_box"/>
        <text x="43" y="52" text-anchor="middle">VFD</text>
        <text x="43" y="90" text-anchor="middle" class="val" id="sv_hz"></text>
        <text x="43" y="110" text-anchor="middle" id="sv_state"></text>
        <line x1="78" y1="85" x2="130" y2="85" stroke="var(--accent)" stroke-width="3" id="sv_cable"/>
        <rect x="130" y="62" width="60" height="46" rx="6" fill="var(--chip)" stroke="var(--ink)" stroke-width="1.5"/>
        <text x="160" y="90" text-anchor="middle">MOTOR</text>
        <text x="160" y="128" text-anchor="middle" class="val" id="sv_rpm"></text>
        <line x1="190" y1="85" x2="218" y2="85" stroke="var(--ink)" stroke-width="4" id="sv_shaft"/>
        <circle cx="265" cy="85" r="46" fill="none" stroke="var(--line)" stroke-width="2"/>
        <g id="vfd_fan" fill="var(--muted)">
          <path d="M265,85 C252,60 265,45 276,48 C283,55 274,72 265,85 Z"/>
          <path d="M265,85 C290,72 305,85 302,96 C295,103 278,94 265,85 Z"/>
          <path d="M265,85 C278,110 265,125 254,122 C247,115 256,98 265,85 Z"/>
          <path d="M265,85 C240,98 225,85 228,74 C235,67 252,76 265,85 Z"/>
          <circle cx="265" cy="85" r="6" fill="var(--ink)"/>
        </g>
        <text x="265" y="150" text-anchor="middle" class="val" id="sv_cfm"></text>
        <text x="265" y="166" text-anchor="middle" id="sv_sp"></text>
        <text x="8" y="170" id="sv_sto"></text>
        <text x="8" y="186" id="sv_ovr"></text>
      </svg>
    </div>
    <table style="margin-top:12px">
      <thead><tr><th>Terminal</th><th>State</th><th>Function</th></tr></thead>
      <tbody id="vfd_io"></tbody>
    </table>
    <table style="margin-top:12px"><tbody id="vfd_act"></tbody></table>`,

    render(s) {
      const g = s.signals, p = s.params, $ = id => document.getElementById(id);
      const mode = g.keypad_mode;
      $('kp_mode').textContent = (g.override_active ? 'OVERRIDE' : mode.toUpperCase()) + (mode === 'auto' && !g.override_active ? ' · EXT1' : '');
      $('kp_dir').textContent = g.running ? '↻ FWD' : '';
      $('kp_state').textContent = g.status_text;
      $('kp_hz').textContent = g.output_freq.toFixed(1);
      $('kp_a').textContent = g.motor_current.toFixed(1);
      $('kp_kw').textContent = g.power_kw.toFixed(1);
      $('kp_ref').textContent = g.ref_hz.toFixed(1);
      $('kp_bus').textContent = g.dc_bus_v.toFixed(0);
      $('kp_temp').textContent = g.drive_temp.toFixed(0);
      $('kp_fault').textContent = g.fault_active ? 'FAULT ' + fault(g.fault_code) : '';
      $('kp_warn').textContent = g.warnings;
      for (const m of ['off', 'hand', 'auto']) $('k_' + m).classList.toggle('sel', mode === m);
      const ref = $('k_ref'); if (document.activeElement !== ref) ref.value = g.hand_ref;

      $('vfd_box').setAttribute('stroke', g.fault_active ? 'var(--bad)' : g.running ? 'var(--ok)' : 'var(--ink)');
      $('sv_hz').textContent = g.output_freq.toFixed(1) + ' Hz';
      $('sv_state').textContent = g.fault_active ? 'FAULT' : g.running ? 'RUN' : 'STOP';
      $('sv_cable').setAttribute('stroke', g.running ? 'var(--accent)' : 'var(--line)');
      $('sv_rpm').textContent = Math.round(g.motor_rpm) + ' rpm';
      const fan = $('vfd_fan'), spd = g.fan_rotating ? Math.max(0.05, g.airflow_cfm / p.design_cfm || g.output_freq / 60) : 0;
      fan.style.animationPlayState = g.fan_rotating ? 'running' : 'paused';
      if (g.fan_rotating) fan.style.animationDuration = (0.35 / spd).toFixed(2) + 's';
      $('sv_cfm').textContent = Math.round(g.airflow_cfm).toLocaleString() + ' cfm';
      $('sv_sp').textContent = g.fan_sp.toFixed(2) + ' in. w.c.';
      $('sv_sto').textContent = s.faults.find(f => f.name === 'sto_open' && f.active) ? 'STO: OPEN (no torque)' : 'STO: closed';
      $('sv_ovr').textContent = p.override_enable ? `Override enabled on ${p.override_source.toUpperCase()}` : '';

      const rows = [];
      for (let i = 1; i <= 6; i++) rows.push(`<tr><td>DI${i}</td><td>${contact(g['di' + i + '_status'])}</td><td>${diFunctions(p, i)}</td></tr>`);
      rows.push(`<tr><td>AI1</td><td>${g.ai1_v.toFixed(2)} V</td><td>${p.ext1_freq_ref1 === 'ai1_scaled'
        ? `Frequency ref (28.11): ${p.ai1_min_v}-${p.ai1_max_v} V = ${p.ai1_scaled_min}-${p.ai1_scaled_max} Hz` : '<span class="muted">not used</span>'}</td></tr>`);
      for (const [i, src] of [[1, p.ro1_source], [2, p.ro2_source], [3, p.ro3_source]])
        rows.push(`<tr><td>RO${i}</td><td>${contact(g['ro' + i])}</td><td>${src} (10.${[24, 27, 30][i - 1]})</td></tr>`);
      rows.push(`<tr><td>AO1</td><td>${(g.ao1_pct / 10).toFixed(2)} V</td><td>${p.ao1_source} (13.12)</td></tr>`);
      $('vfd_io').innerHTML = rows.join('');

      const act = [
        ['Motor speed', Math.round(g.motor_rpm) + ' rpm'], ['Torque', g.torque_pct.toFixed(1) + ' %'],
        ['Output voltage', g.output_voltage.toFixed(0) + ' V'], ['Motor temp (est.)', g.motor_temp.toFixed(0) + ' °C'],
        ['kWh', g.kwh.toFixed(2)], ['Run time', g.run_hours.toFixed(2) + ' h'],
        ['Last fault', fault(g.last_fault)], ['Previous faults', fault(g.prev_fault_1) + ' · ' + fault(g.prev_fault_2)],
        ['Fieldbus idle', s.inputs.efb_idle_s.toFixed(0) + ' s (58.16 = ' + p.comm_loss_time + ' s, ' + p.comm_loss_action + ')'],
      ];
      $('vfd_act').innerHTML = act.map(([k, v]) => `<tr><td>${k}</td><td class="v">${v}</td></tr>`).join('');
    },
  };
})();
