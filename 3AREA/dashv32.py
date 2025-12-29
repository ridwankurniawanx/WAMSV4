from flask import Flask, render_template_string, jsonify, send_file
from influxdb_client import InfluxDBClient
from scipy.signal import butter, filtfilt
import numpy as np
import csv
import os
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

# --- KONFIGURASI INFLUXDB ---
INFLUXDB_CONFIG = {
    'url': 'http://172.17.3.223:8086',
    'token': 'Wo8Rc63Bep01wXaBlM06TgN0TGOvJw4ygVa39dRgcGqwmPs0-aV4aZIv4191xdYJLsjlV2MZvSunX21uU8xITg==',
    'org': 'PLN',
    'bucket': 'pmu_synced_raw',
    'measurement_opt': 'pmu_synced_opt',
    'measurement_ssi': 'pmu_ssi_selected_modes'
}

LOG_FILE = 'wams_events.csv'
# Pelacak status terakhir untuk mencegah double log & mencatat recovery
LAST_LOGGED_STATUS = {'low': 'NORMAL', 'mid': 'NORMAL', 'high': 'NORMAL'}
LOCAL_TZ = timezone(timedelta(hours=7)) # WIB (GMT+7)

# Cache Global untuk fitur Hold Last Value (HLV)
LATEST_SSI_DATA = {
    'low': {"d_akf": 10.0, "d_raw": 10.0, "f": 0.0, "sv": {"medan": "IDLE", "arun": "IDLE"}},
    'mid': {"d_akf": 10.0, "d_raw": 10.0, "f": 0.0, "sv": {"medan": "IDLE", "arun": "IDLE"}},
    'high': {"d_akf": 10.0, "d_raw": 10.0, "f": 0.0, "sv": {"medan": "IDLE", "arun": "IDLE"}}
}

# --- LOGIC: CSV LOGGER ---
def write_event_log(band, severity, freq, damping, source_name):
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, mode='a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['Timestamp', 'Mode', 'Severity', 'Freq (Hz)', 'Damping (%)', 'Source'])
        
        # Menggunakan waktu lokal GMT+7 (WIB)
        timestamp = datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S')
        writer.writerow([timestamp, band.upper(), severity, f"{freq:.3f}", f"{damping:.2f}", source_name])

# --- LOGIC: SSI & SOURCE DETECTION ---
def calculate_sv_standard(p1_arr, p3_arr, f1_arr, band_type):
    if len(p1_arr) < 50: return {"medan": "IDLE", "arun": "IDLE"}
    ranges = {'low': (0.1, 0.9), 'mid': (0.9, 1.3), 'high': (1.3, 2.5)}
    f_low, f_high = ranges[band_type]
    nyq = 0.5 * 25.0
    b, a = butter(4, [f_low / nyq, f_high / nyq], btype='band')
    try:
        d_medan = filtfilt(b, a, p1_arr - np.mean(p1_arr))
        d_arun = filtfilt(b, a, p3_arr - np.mean(p3_arr))
        df = filtfilt(b, a, f1_arr - np.mean(f1_arr))
        e_medan, e_arun = np.sum(d_medan * df), np.sum(d_arun * df)
        energy_threshold = 120 if band_type == 'low' else 60
        noise_floor = 25 
        if e_medan > energy_threshold and e_medan > e_arun:
            return {"medan": "SOURCE", "arun": "VICTIM" if e_arun > noise_floor else "IDLE"}
        if e_arun > energy_threshold and e_arun > e_medan:
            return {"medan": "VICTIM" if e_medan > noise_floor else "IDLE", "arun": "SOURCE"}
    except: pass
    return {"medan": "IDLE", "arun": "IDLE"}

# --- TEMPLATE HTML (DESAIN ASLI ANDA) ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WAMS Professional - Dynamic Monitor</title>
    <style>
        :root {
            --bg-deep: #0b0e14; --panel-bg: #161b22; --header-bg: #0d1117;
            --border-ui: #30363d; --accent-blue: #58a6ff; --text-dim: #8b949e;
            --glow-green: #3fb950; --glow-green-bg: #1b4721;
            --glow-yellow: #d29922; --glow-yellow-bg: #4d3d11;
            --glow-red: #f85149; --glow-red-bg: #6e211e;
            --font-main: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            --font-data: 'Consolas', 'Roboto Mono', monospace;
        }
        body { background-color: var(--bg-deep); color: #c9d1d9; font-family: var(--font-main); margin: 0; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
        header { background: var(--header-bg); padding: 12px 24px; border-bottom: 2px solid var(--border-ui); display: flex; justify-content: space-between; align-items: center; box-shadow: 0 4px 10px rgba(0,0,0,0.3); }
        .logo-box { display: flex; align-items: center; gap: 12px; }
        .logo-tag { border: 2px solid var(--accent-blue); padding: 2px 8px; color: var(--accent-blue); font-weight: 900; font-size: 0.8rem; letter-spacing: 1px; }
        .header-title { font-weight: 700; font-size: 0.9rem; letter-spacing: 0.5px; }
        .header-right { display: flex; align-items: center; gap: 10px; }
        .ui-btn { background: #21262d; border: 1px solid var(--border-ui); color: var(--text-dim); padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 0.7rem; font-weight: bold; transition: 0.2s; text-transform: uppercase; text-decoration: none; display: inline-block; }
        .ui-btn:hover { background: #30363d; color: #fff; }
        #alarm-toggle.enabled { border-color: var(--glow-red); color: var(--glow-red); background: #2a1211; }
        
        .tab-nav { display: flex; background: var(--header-bg); border-bottom: 1px solid var(--border-ui); padding: 0 20px; }
        .tab-btn { padding: 12px 24px; cursor: pointer; border: none; background: none; color: var(--text-dim); font-weight: 600; border-bottom: 2px solid transparent; transition: 0.3s; }
        .tab-btn.active { color: var(--accent-blue); border-bottom-color: var(--accent-blue); }
        .tab-content { flex: 1; display: none; padding: 15px; overflow-y: auto; }
        .tab-content.active { display: block; }

        main { display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; }
        .card { background: var(--panel-bg); border: 1px solid var(--border-ui); border-radius: 12px; padding: 20px; text-align: center; display: flex; flex-direction: column; position: relative; }
        
        .glow-lamp { width: 65px; height: 65px; border-radius: 50%; background: #161b22; margin: 0 auto 12px; transition: 0.5s; border: 4px solid rgba(255,255,255,0.05); }
        .val-trend { font-family: var(--font-data); font-size: 2.2rem; font-weight: 900; line-height: 1; margin-bottom: 5px; }
        .freq-main { font-family: var(--font-data); color: var(--accent-blue); font-weight: bold; font-size: 0.9rem; margin-bottom: 15px; }
        .inst-row { display: flex; justify-content: center; gap: 25px; align-items: center; background: rgba(0,0,0,0.25); padding: 8px 15px; border-radius: 8px; margin: 5px auto 12px; font-family: var(--font-data); font-size: 0.65rem; color: var(--text-dim); border: 1px solid rgba(255,255,255,0.03); width: fit-content; }
        .dot-status { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; background-color: var(--text-dim); transition: 0.3s; }

        .active-ok .glow-lamp { background: var(--glow-green); box-shadow: 0 0 20px var(--glow-green-bg); } .active-ok .val-trend { color: var(--glow-green); }
        .active-warn .glow-lamp { background: var(--glow-yellow); box-shadow: 0 0 25px var(--glow-yellow-bg); animation: pulse 1.5s infinite; } .active-warn .val-trend { color: var(--glow-yellow); }
        .active-crit .glow-lamp { background: var(--glow-red); box-shadow: 0 0 30px var(--glow-red-bg); animation: pulse 0.6s infinite; } .active-crit .val-trend { color: var(--glow-red); }
        
        .sv-box { margin-top: 5px; padding: 8px; background: #0d1117; border-radius: 8px; display: flex; align-items: center; justify-content: space-between; gap: 8px; border: 1px solid var(--border-ui); }
        .sv-label { font-size: 0.6rem; font-weight: 900; padding: 6px 0; border-radius: 4px; text-transform: uppercase; flex: 1; text-align: center; line-height: 1.3; transition: 0.3s; }
        .sv-neutral { background: #21262d; color: var(--text-dim); }
        .sv-source { background: var(--glow-red); color: white; box-shadow: 0 0 12px var(--glow-red-bg); }
        .sv-victim { background: var(--accent-blue); color: white; }

        #sys-clock { font-family: var(--font-data); color: var(--accent-blue); font-weight: 700; font-size: 0.9rem; background: #161b22; padding: 4px 10px; border-radius: 4px; border: 1px solid var(--border-ui); }
        footer { background: var(--header-bg); padding: 8px 24px; border-top: 1px solid var(--border-ui); text-align: center; font-size: 0.7rem; color: var(--text-dim); letter-spacing: 1px; font-weight: 600; }
        @keyframes pulse { 50% { opacity: 0.6; } }

        table { width:100%; border-collapse:collapse; font-family:var(--font-data); font-size:0.8rem; }
        th { text-align: left; color: var(--accent-blue); border-bottom: 2px solid var(--border-ui); padding: 10px; }
        td { padding: 8px 10px; border-bottom: 1px solid var(--border-ui); }
    </style>
</head>
<body>
    <header>
        <div class="logo-box">
            <div class="logo-tag">WAMS</div>
            <div class="header-title">ARUN - MEDAN INTERCONNECTION STABILITY MONITOR</div>
        </div>
        <div class="header-right">
            <button id="test-btn" class="ui-btn" onclick="testSound()">Test Sound</button>
            <button id="alarm-toggle" class="ui-btn" onclick="toggleAlarm()">🔈 Audio Off</button>
            <div id="sys-clock">00:00:00</div>
        </div>
    </header>

    <div class="tab-nav">
        <div class="tab-btn active" onclick="switchTab('monitor')">REAL-TIME MONITOR</div>
        <div class="tab-btn" onclick="switchTab('logs')">EVENT LOGS <span id="log-badge" style="display:none; color:#f85149; margin-left:5px;">●</span></div>
    </div>

    <div id="tab-monitor" class="tab-content active">
        <main>
            {% for b in [('low', 'Inter-Area Mode'), ('mid', 'Transition Mode'), ('high', 'Local Mode')] %}
            <div id="card-{{b[0]}}" class="card">
                <div style="font-size:0.65rem; color:var(--text-dim); text-transform:uppercase; margin-bottom:15px; font-weight:800; letter-spacing:1.5px; border-bottom:1px solid var(--border-ui); padding-bottom:8px;">{{b[1]}}</div>
                <div class="glow-lamp"></div>
                <div class="val-trend" id="val-{{b[0]}}">--</div>
                <div id="freq-{{b[0]}}" class="freq-main">F: -- Hz</div>
                <div class="inst-row">
                    <span style="display:flex; align-items:center;">
                        <span id="dot-{{b[0]}}" class="dot-status"></span>
                        INS D:<span class="inst-val" id="inst-d-{{b[0]}}">--</span>%
                    </span>
                    <span>INS F:<span class="inst-val" id="inst-f-{{b[0]}}">--</span> Hz</span>
                </div>
                <div class="sv-box">
                    <span id="sv-arun-{{b[0]}}" class="sv-label sv-neutral">ARUN</span>
                    <span style="color:var(--border-ui); font-weight:bold;">⇌</span>
                    <span id="sv-medan-{{b[0]}}" class="sv-label sv-neutral">MEDAN</span>
                </div>
            </div>
            {% endfor %}
        </main>
    </div>

    <div id="tab-logs" class="tab-content">
        <div style="margin-bottom: 10px; display: flex; justify-content: flex-end;">
            <a href="/download/logs" class="ui-btn" style="background: var(--glow-green-bg); color: white; border-color: var(--glow-green);">📥 Download CSV</a>
        </div>
        <div style="background:var(--panel-bg); border:1px solid var(--border-ui); border-radius:8px; padding:15px;">
            <table>
                <thead>
                    <tr><th>Timestamp</th><th>Mode</th><th>Severity</th><th>Freq</th><th>Damping</th><th>Source</th></tr>
                </thead>
                <tbody id="log-body"></tbody>
            </table>
        </div>
    </div>

    <footer>Copyright @2025 Ridwan</footer>

    <script>
        let alarmEnabled = false;
        let audioCtx = null;
        let lastWarningPlay = 0;

        function initAudio() { if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
        function toggleAlarm() { initAudio(); alarmEnabled = !alarmEnabled; document.getElementById('alarm-toggle').innerHTML = alarmEnabled ? '🔊 Audio On' : '🔈 Audio Off'; document.getElementById('alarm-toggle').classList.toggle('enabled', alarmEnabled); }

        function playAlarmSound(severity) {
            if (!audioCtx || !alarmEnabled) return;
            if (audioCtx.state === 'suspended') audioCtx.resume();
            const now = Date.now();

            if (severity === 'CRITICAL') {
                // MEGA ALARM: 3x Teeet... Teeet... Teeet...
                const pattern = [0, 0.6, 1.2]; 
                pattern.forEach(delay => {
                    const t = audioCtx.currentTime + delay;
                    const osc = audioCtx.createOscillator();
                    const gain = audioCtx.createGain();
                    osc.type = 'sawtooth';
                    osc.frequency.setValueAtTime(1100, t);
                    osc.frequency.exponentialRampToValueAtTime(1300, t + 0.4);
                    gain.gain.setValueAtTime(0.2, t);
                    gain.gain.linearRampToValueAtTime(0, t + 0.45);
                    osc.connect(gain); gain.connect(audioCtx.destination);
                    osc.start(t); osc.stop(t + 0.5);
                });
            } else if (severity === 'WARNING' && (now - lastWarningPlay > 5000)) {
                const t = audioCtx.currentTime;
                const osc = audioCtx.createOscillator(); const gain = audioCtx.createGain();
                osc.type = 'sine'; osc.frequency.setValueAtTime(440, t);
                gain.gain.setValueAtTime(0.1, t); gain.gain.exponentialRampToValueAtTime(0.001, t + 0.5);
                osc.connect(gain); gain.connect(audioCtx.destination);
                osc.start(t); osc.stop(t + 0.5);
                lastWarningPlay = now;
            }
        }

        async function updateData() {
            try {
                const response = await fetch('/api/data');
                const data = await response.json();
                let highestSeverity = 'NORMAL';
                ['low', 'mid', 'high'].forEach(band => {
                    const info = data[band];
                    if (info) {
                        updateCard(band, info);
                        if (info.d_akf < 3.0) highestSeverity = 'CRITICAL';
                        else if (info.d_akf < 5.0 && highestSeverity !== 'CRITICAL') highestSeverity = 'WARNING';
                    }
                });
                if (highestSeverity !== 'NORMAL') playAlarmSound(highestSeverity);
            } catch (err) { console.error(err); }
        }

        function updateSVLabel(elId, status, locName) {
            const el = document.getElementById(elId);
            el.innerHTML = `<div style="font-size:0.7rem">${locName}</div><div style="font-size: 0.45rem; opacity: 0.8;">${status}</div>`;
            el.className = "sv-label " + (status === "SOURCE" ? "sv-source" : (status === "VICTIM" ? "sv-victim" : "sv-neutral"));
        }

        function updateCard(band, info) {
            document.getElementById(`val-${band}`).innerText = info.d_akf.toFixed(2) + "%";
            document.getElementById(`freq-${band}`).innerText = `F: ${info.f.toFixed(3)} Hz`;
            document.getElementById(`inst-f-${band}`).innerText = info.f.toFixed(3);
            const instD = document.getElementById(`inst-d-${band}`);
            const dot = document.getElementById(`dot-${band}`);
            const color = info.d_raw < 3.0 ? "var(--glow-red)" : (info.d_raw < 5.0 ? "var(--glow-yellow)" : "var(--glow-green)");
            instD.innerText = info.d_raw.toFixed(1); instD.style.color = color; dot.style.backgroundColor = color;
            document.getElementById(`card-${band}`).className = "card " + (info.d_akf < 3.0 ? "active-crit" : (info.d_akf < 5.0 ? "active-warn" : "active-ok"));
            updateSVLabel(`sv-arun-${band}`, info.sv.arun, "ARUN");
            updateSVLabel(`sv-medan-${band}`, info.sv.medan, "MEDAN");
        }

        async function loadLogs() {
            const res = await fetch('/api/logs');
            const logs = await res.json();
            const tbody = document.getElementById('log-body');
            tbody.innerHTML = logs.map(l => `
                <tr>
                    <td>${l.Timestamp}</td>
                    <td>${l.Mode}</td>
                    <td style="color:${l.Severity.includes('CRITICAL') ? 'var(--glow-red)' : (l.Severity.includes('WARNING') ? 'var(--glow-yellow)' : 'var(--glow-green)')}">${l.Severity}</td>
                    <td>${l['Freq (Hz)']} Hz</td>
                    <td>${l['Damping (%)']}%</td>
                    <td style="font-weight:bold; color:var(--accent-blue)">${l.Source}</td>
                </tr>
            `).join('');
        }

        function switchTab(tab) {
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.getElementById('tab-' + tab).classList.add('active');
            event.currentTarget.classList.add('active');
            if(tab === 'logs') { loadLogs(); document.getElementById('log-badge').style.display = 'none'; }
        }

        setInterval(() => { document.getElementById('sys-clock').innerText = new Date().toLocaleTimeString('id-ID', {hour12: false}); }, 1000);
        setInterval(updateData, 5000); updateData();
        function testSound() { initAudio(); playAlarmSound('WARNING'); setTimeout(() => playAlarmSound('CRITICAL'), 1000); }
    </script>
</body>
</html>
"""

# --- BACKEND LOGIC (SMART LOGGING & HLV) ---

def get_influx_data():
    global LATEST_SSI_DATA, LAST_LOGGED_STATUS
    client = InfluxDBClient(url=INFLUXDB_CONFIG['url'], token=INFLUXDB_CONFIG['token'], org=INFLUXDB_CONFIG['org'])
    q_api = client.query_api()
    
    q_raw = f'from(bucket: "{INFLUXDB_CONFIG["bucket"]}") |> range(start: -25s) |> filter(fn: (r) => r._measurement == "{INFLUXDB_CONFIG["measurement_opt"]}") |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")'
    q_ssi = f'from(bucket: "{INFLUXDB_CONFIG["bucket"]}") |> range(start: -1h) |> filter(fn: (r) => r._measurement == "{INFLUXDB_CONFIG["measurement_ssi"]}") |> last() |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")'
    
    try:
        # Get Raw for Source Detection
        tables_raw = q_api.query(q_raw)
        p_medan, p_arun, f_sys = [], [], []
        if tables_raw:
            for r in tables_raw[0].records:
                v_m, v_a, f = r.values.get('totw_pmu_1'), r.values.get('totw_pmu_3'), r.values.get('freq_PMU_1')
                if None not in [v_m, v_a, f]:
                    p_medan.append(float(v_m)); p_arun.append(float(v_a)); f_sys.append(float(f))
        m_arr, a_arr, f_arr = np.array(p_medan), np.array(p_arun), np.array(f_sys)

        # Get SSI Data
        tables = q_api.query(q_ssi)
        if tables and len(tables[0].records) > 0:
            rec = tables[0].records[0]
            for band in ['low', 'mid', 'high']:
                d_akf_raw = rec.values.get(f'{band}_band_d_akf')
                freq_raw = rec.values.get(f'{band}_band_f')
                d_inst_raw = rec.values.get(f'{band}_band_d')
                
                # --- LOGIKA HOLD LAST VALUE (HLV) ---
                if d_akf_raw is not None and freq_raw is not None:
                    d_akf = float(d_akf_raw)
                    freq = float(freq_raw)
                    d_raw = float(d_inst_raw) if d_inst_raw is not None else 10.0
                    
                    sv_result = calculate_sv_standard(m_arr, a_arr, f_arr, band)
                    source_detected = "ARUN" if sv_result['arun'] == "SOURCE" else ("MEDAN" if sv_result['medan'] == "SOURCE" else "UNKNOWN/EXT")
                    
                    # Logika Penentuan Status
                    current_status = 'NORMAL'
                    if d_akf < 3.0: current_status = 'CRITICAL'
                    elif d_akf < 5.0: current_status = 'WARNING'
                    
                    # --- SMART LOGGING (ANTI-DOUBLE & RECOVERY) ---
                    if current_status != LAST_LOGGED_STATUS[band]:
                        # A. Jika berubah ke Anomali (Warning/Critical)
                        if current_status != 'NORMAL':
                            write_event_log(band, current_status, freq, d_akf, source_detected)
                        # B. Jika berubah kembali ke Normal dari Anomali (Recovery)
                        elif current_status == 'NORMAL' and LAST_LOGGED_STATUS[band] != 'NORMAL':
                            write_event_log(band, "NORMAL (RECOVERED)", freq, d_akf, "SYSTEM")
                        
                        LAST_LOGGED_STATUS[band] = current_status
                    
                    # Update Global Cache
                    LATEST_SSI_DATA[band] = {"d_akf": d_akf, "d_raw": d_raw, "f": freq, "sv": sv_result}
                else:
                    # Data None? Diamkan saja, gunakan nilai lama dari LATEST_SSI_DATA (HLV)
                    pass
    except Exception as e:
        print(f"DB Error: {e}") 
    finally:
        client.close()
    return LATEST_SSI_DATA

@app.route('/')
def index(): return render_template_string(HTML_TEMPLATE)

@app.route('/api/data')
def api_data(): return jsonify(get_influx_data())

@app.route('/api/logs')
def get_logs():
    if not os.path.exists(LOG_FILE): return jsonify([])
    with open(LOG_FILE, mode='r') as f:
        return jsonify(list(csv.DictReader(f))[-50:][::-1])

@app.route('/download/logs')
def download_logs():
    return send_file(LOG_FILE, as_attachment=True) if os.path.exists(LOG_FILE) else ("Not found", 404)

if __name__ == '__main__':
    # IP disesuaikan dengan server Anda
    app.run(host='172.17.3.224', port=5000, debug=False)
