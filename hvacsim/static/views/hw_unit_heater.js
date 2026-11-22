// Equipment view: hot water unit heater
VIEWS.hw_unit_heater = {
  html: `
      <svg viewBox="0 0 640 360" role="img" aria-label="Hot water unit heater schematic">
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M0,0 L10,5 L0,10 z" fill="var(--warn)"/>
          </marker>
        </defs>
        <!-- HW supply line -->
        <text x="18" y="70" class="svglabel">HW SUPPLY</text>
        <line id="pipe_branch" x1="18" y1="84" x2="150" y2="84" stroke-width="10" stroke-linecap="round"/>
        <line id="pipe_supply" x1="190" y1="84" x2="330" y2="84" stroke-width="10"/>
        <line id="pipe_supply2" x1="330" y1="84" x2="330" y2="120" stroke-width="10"/>
        <!-- valve (bowtie) -->
        <g id="valve">
          <path d="M150,70 L190,98 L190,70 L150,98 Z" stroke="var(--ink)" stroke-width="1.5" id="valvebody"/>
          <rect x="160" y="40" width="20" height="22" rx="3" fill="var(--chip)" stroke="var(--ink)" stroke-width="1.2"/>
          <line x1="170" y1="62" x2="170" y2="84" stroke="var(--ink)" stroke-width="1.5"/>
        </g>
        <text x="146" y="122" class="svglabel">2-POS VALVE</text>
        <text x="146" y="138" class="svgtext" id="t_valve"></text>
        <!-- aquastat -->
        <rect x="236" y="72" width="44" height="24" rx="4" fill="var(--chip)" stroke="var(--ink)" stroke-width="1.2" id="aq_box"/>
        <text x="258" y="88" text-anchor="middle" class="svglabel">AQ</text>
        <text x="216" y="30" class="svglabel" id="t_aqsp">AQUASTAT</text>
        <text x="216" y="48" class="svgtext" id="t_aq"></text>
        <text x="216" y="114" class="svgtext" id="t_pipe"></text>
        <!-- unit heater cabinet -->
        <rect x="300" y="120" width="190" height="150" rx="6" fill="none" stroke="var(--ink)" stroke-width="2"/>
        <!-- coil -->
        <rect id="coil" x="314" y="132" width="34" height="126" rx="3"/>
        <g stroke="var(--panel)" stroke-width="1.5" opacity=".7">
          <line x1="314" y1="146" x2="348" y2="146"/><line x1="314" y1="160" x2="348" y2="160"/>
          <line x1="314" y1="174" x2="348" y2="174"/><line x1="314" y1="188" x2="348" y2="188"/>
          <line x1="314" y1="202" x2="348" y2="202"/><line x1="314" y1="216" x2="348" y2="216"/>
          <line x1="314" y1="230" x2="348" y2="230"/><line x1="314" y1="244" x2="348" y2="244"/>
        </g>
        <text x="331" y="286" text-anchor="middle" class="svglabel">COIL</text>
        <text x="331" y="302" text-anchor="middle" class="svgtext" id="t_coil"></text>
        <!-- fan -->
        <circle cx="420" cy="195" r="52" fill="none" stroke="var(--line)" stroke-width="2"/>
        <g id="fanblades" fill="var(--muted)">
          <path d="M420,195 C405,165 420,148 432,152 C440,160 430,180 420,195 Z"/>
          <path d="M420,195 C450,180 467,195 463,207 C455,215 435,205 420,195 Z"/>
          <path d="M420,195 C435,225 420,242 408,238 C400,230 410,210 420,195 Z"/>
          <path d="M420,195 C390,210 373,195 377,183 C385,175 405,185 420,195 Z"/>
          <circle cx="420" cy="195" r="7" fill="var(--ink)"/>
        </g>
        <!-- motor + CT -->
        <rect x="500" y="178" width="46" height="34" rx="5" fill="var(--chip)" stroke="var(--ink)" stroke-width="1.2" id="motor"/>
        <text x="523" y="199" text-anchor="middle" class="svglabel">M</text>
        <line x1="546" y1="195" x2="600" y2="195" stroke="var(--ink)" stroke-width="1.5"/>
        <circle cx="575" cy="195" r="9" fill="none" stroke="var(--accent)" stroke-width="3"/>
        <text x="560" y="174" class="svglabel">CT 0-10 A</text>
        <text x="560" y="230" class="svgbig" id="t_amps"></text>
        <text x="500" y="250" class="svgtext" id="t_motor"></text>
        <!-- warm air -->
        <g id="air" color="var(--warn)" stroke="var(--warn)" stroke-width="2.5" fill="none">
          <line x1="360" y1="290" x2="360" y2="330" marker-end="url(#arrow)"/>
          <line x1="420" y1="290" x2="420" y2="335" marker-end="url(#arrow)"/>
          <line x1="480" y1="290" x2="480" y2="330" marker-end="url(#arrow)"/>
        </g>
        <!-- space sensor -->
        <rect x="40" y="220" width="60" height="70" rx="8" fill="var(--chip)" stroke="var(--ink)" stroke-width="1.2"/>
        <text x="70" y="250" text-anchor="middle" class="svgbig" id="t_space"></text>
        <text x="70" y="272" text-anchor="middle" class="svglabel">SPACE</text>
        <text x="40" y="312" class="svglabel" id="t_space_actual"></text>
        <text x="40" y="330" class="svglabel" id="t_oat"></text>
        <text x="300" y="350" class="svgtext" id="t_q"></text>
      </svg>
`,
  render(s) {
    const g = s.signals;
  // schematic
  $('pipe_branch').setAttribute('stroke', tempColor(g.hw_branch_temp));
  $('pipe_supply').setAttribute('stroke', tempColor(g.pipe_temp));
  $('pipe_supply2').setAttribute('stroke', tempColor(g.pipe_temp));
  $('coil').setAttribute('fill', tempColor(g.coil_temp));
  $('valvebody').setAttribute('fill', g.valve_position > 50 ? 'var(--ok)' : 'var(--chip)');
  $('t_valve').textContent = `${g.valve_position.toFixed(0)}% open`;
  $('t_pipe').textContent = `${g.pipe_temp.toFixed(1)}°F`;
  $('t_aqsp').textContent = `AQUASTAT on ${s.params.aquastat_close_temp}°F / off ${s.params.aquastat_open_temp}°F`;
  $('aq_box').setAttribute('fill', g.aquastat_closed ? 'color-mix(in srgb, var(--ok) 35%, transparent)' : 'var(--chip)');
  $('t_aq').textContent = g.aquastat_closed ? 'CLOSED - fan enabled' : 'OPEN';
  $('t_coil').textContent = `${g.coil_temp.toFixed(0)}°F`;
  const fb = $('fanblades');
  fb.classList.toggle('run', g.fan_speed > 80);
  fb.classList.toggle('coast', g.fan_speed > 1 && g.fan_speed <= 80);
  $('t_amps').textContent = `${g.fan_amps.toFixed(2)} A`;
  $('t_motor').textContent = g.protector_tripped ? 'THERMAL PROTECTOR OPEN'
    : g.motor_energized ? (g.fan_running ? 'motor running' : 'motor energized, not turning') : 'motor off';
  $('motor').setAttribute('stroke', g.protector_tripped ? 'var(--bad)' : 'var(--ink)');
  $('air').style.opacity = Math.max(0.08, Math.min(1, g.heat_output / 20000) * (g.fan_speed / 100));
  $('t_space').textContent = `${g.space_temp.toFixed(1)}°`;
  $('t_space_actual').textContent = `actual ${g.space_temp_actual.toFixed(1)}°F`;
  $('t_oat').textContent = `OA ${g.oat.toFixed(0)}°F`;
  $('t_q').textContent = `heat output ${Math.round(g.heat_output).toLocaleString()} BTU/h`;
  },
};
