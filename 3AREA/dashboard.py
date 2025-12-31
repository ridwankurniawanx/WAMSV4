from flask import Flask, render_template_string, jsonify, request, redirect, url_for
from influxdb_client import InfluxDBClient
from scipy.signal import butter, filtfilt, hilbert
import numpy as np
import os
import json
from datetime import datetime

app = Flask(__name__)

# --- 1. KONFIGURASI FILE & DEFAULT ---
CONFIG_FILE = 'alarm_config.json'
DEFAULT_CONFIG = {
    'warn_val': 5.0,
    'crit_val': 3.0,
    'logic_mode': 'akf',
    'amp_medan': 0.05,
    'amp_nagan': 0.05,
    'amp_arun': 0.05
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return DEFAULT_CONFIG

def save_config(data):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(data, f, indent=4)

ALARM_SETTINGS = load_config()

INFLUXDB_CONFIG = {
    'url': 'http://172.17.3.223:8086',
    'token': 'Wo8Rc63Bep01wXaBlM06TgN0TGOvJw4ygVa39dRgcGqwmPs0-aV4aZIv4191xdYJLsjlV2MZvSunX21uU8xITg==',
    'org': 'PLN',
    'bucket': 'pmu_synced_raw',
    'measurement_opt': 'pmu_synced_opt',
    'measurement_ssi': 'pmu_ssi_selected_modes'
}

# --- 2. FUNGSI PENDUKUNG ---

def lttb_downsample(data, threshold):
    if threshold >= len(data) or threshold <= 2:
        return data
    sampled = [data[0]]
    bucket_size = (len(data) - 2) / (threshold - 2)
    for i in range(threshold - 2):
        avg_range_start = int(np.floor((i + 1) * bucket_size) + 1)
        avg_range_end = int(np.floor((i + 2) * bucket_size) + 1)
        avg_range_end = min(avg_range_end, len(data))
        avg_bucket = data[avg_range_start:avg_range_end]
        avg_x = np.mean([datetime.fromisoformat(d['time']).timestamp() for d in avg_bucket])
        avg_y = np.mean([d['d_akf'] for d in avg_bucket])
        curr_range_start = int(np.floor(i * bucket_size) + 1)
        curr_range_end = int(np.floor((i + 1) * bucket_size) + 1)
        prev_point = sampled[-1]
        prev_x = datetime.fromisoformat(prev_point['time']).timestamp()
        prev_y = prev_point['d_akf']
        max_area = -1
        best_point = data[curr_range_start]
        for j in range(curr_range_start, curr_range_end):
            p_x = datetime.fromisoformat(data[j]['time']).timestamp()
            p_y = data[j]['d_akf']
            area = abs(0.5 * (prev_x * (p_y - avg_y) + p_x * (avg_y - prev_y) + avg_x * (prev_y - p_y)))
            if area > max_area:
                max_area = area
                best_point = data[j]
        sampled.append(best_point)
    sampled.append(data[-1])
    return sampled

def calculate_hilbert_amp(data_arr, f_min, f_max):
    clean = data_arr[~np.isnan(data_arr)]
    if len(clean) < 500: return 0.0
    try:
        nyq = 12.5 
        b, a = butter(2, [f_min/nyq, f_max/nyq], btype='band')
        filt = filtfilt(b, a, clean - np.mean(clean))
        amp = np.abs(hilbert(filt))
        return float(np.max(amp[-250:]) * 2.0) / 1000000.0
    except: return 0.0

def calculate_sv_standard(p1_arr, p2_arr, p3_arr, f1_arr, band_type):
    mask = ~np.isnan(p1_arr) & ~np.isnan(p2_arr) & ~np.isnan(p3_arr) & ~np.isnan(f1_arr)
    p1, p2, p3, f_ref = p1_arr[mask], p2_arr[mask], p3_arr[mask], f1_arr[mask]
    if len(p1) < 100: return {"medan": "IDLE", "nagan": "IDLE", "arun": "IDLE"}
    ranges = {'low': (0.1, 0.9), 'mid': (0.9, 1.3), 'high': (1.3, 2.5)}
    f_l, f_h = ranges[band_type]
    try:
        b, a = butter(4, [f_l/12.5, f_h/12.5], btype='band')
        df = filtfilt(b, a, f_ref - np.mean(f_ref))
        energies = {}
        for name, p_data in [('medan', p1), ('nagan', p2), ('arun', p3)]:
            dp = filtfilt(b, a, p_data - np.mean(p_data))
            if np.mean(p_data) < 0: dp *= -1.0
            dp_pu = (dp / abs(np.mean(p_data)) * 100) if abs(np.mean(p_data)) > 5.0 else np.zeros_like(dp)
            energies[name] = np.sum(dp_pu * df)
        max_s = max(energies, key=energies.get)
        res = {"medan": "IDLE", "nagan": "IDLE", "arun": "IDLE"}
        if energies[max_s] > 0.01:
            res[max_s] = "SOURCE"
            for k in res:
                if k != max_s: res[k] = "VICTIM" if energies[k] < -0.001 else "IDLE"
        return res
    except: return {"medan": "IDLE", "nagan": "IDLE", "arun": "IDLE"}

# --- 3. TEMPLATES ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <title>WAMS Professional Monitor</title>
    <style>
        :root {
            --bg-deep: #0b0e14; --panel-bg: #161b22; --header-bg: #0d1117;
            --border-ui: #30363d; --accent-blue: #58a6ff; --text-dim: #8b949e;
            --glow-green: #3fb950; --glow-yellow: #d29922; --glow-red: #f85149;
        }
        body { background: var(--bg-deep); color: #c9d1d9; font-family: 'Segoe UI', sans-serif; margin: 0; overflow: hidden; height: 100vh; display: flex; flex-direction: column; }
        header { background: var(--header-bg); padding: 12px 24px; border-bottom: 2px solid var(--border-ui); display: flex; justify-content: space-between; align-items: center; }
        
        .sub-header { 
            background: #1c2128; 
            border-bottom: 1px solid var(--border-ui); 
            padding: 12px 24px; 
            display: none; 
            align-items: center; 
            gap: 30px; 
            font-size: 0.8rem; 
            color: var(--text-dim);
            text-transform: uppercase;
            letter-spacing: 1px;
            font-weight: 600;
        }
        .sub-header b { color: var(--accent-blue); margin-left: 6px; }
        .status-dot { width: 10px; height: 10px; background: var(--glow-green); border-radius: 50%; display: inline-block; margin-right: 8px; box-shadow: 0 0 10px var(--glow-green); }
        .logo-tag { border: 2px solid var(--accent-blue); padding: 2px 8px; color: var(--accent-blue); font-weight: 900; letter-spacing: 1px; }
        
        main { display: grid; grid-template-columns: 1fr 2fr 1fr; gap: 15px; padding: 15px; flex: 1; min-height: 0; }
        .card { background: var(--panel-bg); border: 1px solid var(--border-ui); border-radius: 12px; padding: 20px; text-align: center; display: flex; flex-direction: column; transition: 0.3s; box-shadow: 0 4px 20px rgba(0,0,0,0.4); overflow: hidden; }
        
        body.is-fullscreen .card { padding: 10px 20px; }
        body.is-fullscreen .chart-container { 
            height: 310px !important; 
            margin-top: 5px; 
        }

        .glow-lamp { width: 68px; height: 68px; border-radius: 50%; background: #161b22; margin: 0 auto 12px; border: 4px solid rgba(255,255,255,0.05); }

        .active-warn { border: 2px solid var(--glow-yellow) !important; animation: sync-border-warn 2.0s infinite ease-in-out; }
        .d-warn .glow-lamp { background: var(--glow-yellow); animation: sync-lamp-warn 2.0s infinite ease-in-out; }
        @keyframes sync-border-warn {
            0%, 100% { box-shadow: 0 0 5px rgba(210, 153, 34, 0.2); border-color: var(--glow-yellow); }
            50% { box-shadow: 0 0 25px rgba(210, 153, 34, 0.6); border-color: #ffca28; }
        }
        @keyframes sync-lamp-warn {
            0%, 100% { opacity: 0.5; box-shadow: 0 0 5px rgba(210, 153, 34, 0.2); }
            50% { opacity: 1; box-shadow: 0 0 30px rgba(210, 153, 34, 0.8); }
        }

        .active-crit { border: 2px solid var(--glow-red) !important; animation: sync-border-crit 0.6s infinite alternate ease-in-out; }
        .d-crit .glow-lamp { background: var(--glow-red); animation: sync-lamp-crit 0.6s infinite alternate ease-in-out; }
        @keyframes sync-border-crit {
            from { box-shadow: 0 0 10px rgba(248, 81, 73, 0.3); border-color: var(--glow-red); }
            to { box-shadow: 0 0 40px rgba(248, 81, 73, 0.9); border-color: #ff1744; }
        }
        @keyframes sync-lamp-crit {
            from { opacity: 0.4; box-shadow: 0 0 10px rgba(248, 81, 73, 0.3); }
            to { opacity: 1; box-shadow: 0 0 45px rgba(248, 81, 73, 1); }
        }

        .active-ok { border: 1px solid var(--border-ui) !important; box-shadow: none !important; animation: none !important; }
        .d-ok .glow-lamp { background: var(--glow-green); box-shadow: 0 0 25px #1b4721; animation: none !important; opacity: 1; }

        .val-trend { font-family: 'Consolas', monospace; font-size: 2.4rem; font-weight: 900; line-height: 1.1; }
        .d-ok .val-trend { color: var(--glow-green); }
        .d-warn .val-trend { color: var(--glow-yellow); }
        .d-crit .val-trend { color: var(--glow-red); }
        
        .inst-row { display: flex; justify-content: center; gap: 18px; background: rgba(0,0,0,0.3); padding: 8px 12px; border-radius: 8px; font-size: 0.7rem; margin-top: 10px; border: 1px solid rgba(255,255,255,0.03); }
        .dot-mini { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 6px; border: 1px solid rgba(255,255,255,0.2); }
        .sv-box { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 6px; margin-top: 15px; }
        .sv-label { font-size: 0.65rem; font-weight: 900; padding: 6px; background: #21262d; border-radius: 4px; color: var(--text-dim); }
        .sv-source { background: #6e211e !important; color: white !important; border: 1px solid var(--glow-red); }
        .sv-victim { background: #4d3d11 !important; color: white !important; border: 1px solid var(--glow-yellow); }
        .amp-val { font-family: 'Consolas', monospace; font-size: 0.65rem; color: var(--glow-yellow); margin-top: 6px; border-top: 1px solid #30363d; padding-top: 4px; font-weight: bold; }
        #sys-clock { font-family: 'Consolas', monospace; color: var(--accent-blue); font-weight: bold; padding: 4px 10px; background: #0d1117; border-radius: 4px; border: 1px solid var(--border-ui); }
        
        .chart-container { margin-top: auto; height: 180px; position: relative; border-top: 1px solid var(--border-ui); padding-top: 15px; }
        .time-select { position: absolute; top: 12px; right: 5px; background: #0d1117; color: var(--accent-blue); border: 1px solid var(--border-ui); font-size: 0.6rem; border-radius: 4px; z-index: 10; cursor: pointer; }
        .hide-toggle { position: absolute; top: 12px; left: 0px; color: var(--text-dim); font-size: 0.6rem; z-index: 10; display: flex; align-items: center; gap: 4px; cursor: pointer; }
        canvas { width: 100%; height: 100%; display: block; }

        #configModal { display:none; position:fixed; z-index:100; left:0; top:0; width:100%; height:100%; background:rgba(0,0,0,0.8); }
        .modal-content { background:var(--panel-bg); margin:10% auto; padding:25px; border:1px solid var(--accent-blue); border-radius:12px; width:400px; }
        .modal-content h3 { margin-top:0; color:var(--accent-blue); border-bottom:1px solid var(--border-ui); padding-bottom:10px; }
        .form-group { margin-bottom:15px; }
        .form-group label { display:block; font-size:0.75rem; color:var(--text-dim); margin-bottom:5px; }
        .form-group input, .form-group select { width:100%; padding:8px; background:#0d1117; border:1px solid var(--border-ui); color:white; border-radius:4px; box-sizing: border-box; }
        .btn-save { background:var(--accent-blue); color:white; border:none; padding:10px 20px; border-radius:4px; cursor:pointer; font-weight:bold; width:100%; }
        
        .audio-toggle { background: #21262d; color: #58a6ff; border: 1px solid var(--border-ui); padding: 4px 10px; border-radius: 4px; font-size: 0.65rem; cursor: pointer; font-weight: bold; }
        
        /* FOOTER STYLE */
        footer { 
            background: var(--header-bg); 
            border-top: 1px solid var(--border-ui); 
            padding: 8px; 
            text-align: center; 
            font-size: 0.65rem; 
            color: var(--text-dim); 
            letter-spacing: 0.5px;
        }
    </style>
</head>
<body>
    <header>
        <div style="font-weight:700;"><span class="logo-tag">WAMS</span> SUMATERA SYSTEM WIDE MONITOR</div>
        <div class="header-right" style="display:flex; align-items:center; gap:10px;">
            <button id="btnAudio" class="audio-toggle" onclick="toggleAudio()">ENABLE AUDIO</button>
            <a href="javascript:void(0)" onclick="openModal()" style="color:var(--glow-yellow); text-decoration:none; font-size:0.7rem; border:1px solid; padding:4px 8px; border-radius:4px;">CONFIG</a>
            <div id="sys-clock">00:00:00</div>
        </div>
    </header>

    <div id="sub-header-bar" class="sub-header">
        <span><span class="status-dot"></span> ANALYTICS: <b>REAL-TIME ONLINE</b></span>
        <span>LOGIC: <b>{{cfg.logic_mode.upper()}}</b></span>
        <span>THRESHOLD: <b>{{cfg.warn_val}}% / {{cfg.crit_val}}%</b></span>
        <span style="margin-left:auto; color:var(--accent-blue);">F11 MONITORING ACTIVE</span>
    </div>

    <div id="configModal">
        <div class="modal-content">
            <h3>ALARM CONFIGURATION</h3>
            <form action="/save-config" method="POST">
                <div class="form-group">
                    <label>Logika Damping</label>
                    <select name="logic_mode">
                        <option value="akf" {% if cfg.logic_mode == 'akf' %}selected{% endif %}>Hanya Damping AKF</option>
                        <option value="raw" {% if cfg.logic_mode == 'raw' %}selected{% endif %}>Hanya Damping Raw (Inst.)</option>
                        <option value="both" {% if cfg.logic_mode == 'both' %}selected{% endif %}>AKF AND Raw (Keduanya)</option>
                    </select>
                </div>
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
                    <div class="form-group">
                        <label>Low Damping (%) - Orange</label>
                        <input type="number" step="0.1" name="warn_val" value="{{cfg.warn_val}}">
                    </div>
                    <div class="form-group">
                        <label>Poor Damping (%) - Red</label>
                        <input type="number" step="0.1" name="crit_val" value="{{cfg.crit_val}}">
                    </div>
                </div>
                <div style="font-size:0.7rem; color:var(--accent-blue); margin-bottom:10px;">AMP THRESHOLD (MW)</div>
                <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:5px;">
                    <div class="form-group">
                        <label>Medan</label>
                        <input type="number" step="0.01" name="amp_medan" value="{{cfg.amp_medan}}">
                    </div>
                    <div class="form-group">
                        <label>Nagan</label>
                        <input type="number" step="0.01" name="amp_nagan" value="{{cfg.amp_nagan}}">
                    </div>
                    <div class="form-group">
                        <label>Arun</label>
                        <input type="number" step="0.01" name="amp_arun" value="{{cfg.amp_arun}}">
                    </div>
                </div>
                <button type="submit" class="btn-save">SAVE & REFRESH</button>
                <button type="button" onclick="closeModal()" style="background:transparent; color:var(--text-dim); border:none; width:100%; margin-top:10px; cursor:pointer;">Cancel</button>
            </form>
        </div>
    </div>

    <main>
        {% for b in [('mid', 'Transition Mode'), ('low', 'Inter-Area Mode'), ('high', 'Local Mode')] %}
        <div id="card-{{b[0]}}" class="card">
            <div style="font-size:0.65rem; color:var(--text-dim); font-weight:800; margin-bottom:12px; letter-spacing:1px; border-bottom:1px solid #30363d; padding-bottom:5px;">{{b[1].upper()}}</div>
            
            <div class="glow-lamp"></div>
            <div class="val-trend" id="val-{{b[0]}}">--%</div>
            <div id="freq-{{b[0]}}" style="color:var(--accent-blue); font-weight:bold; margin-bottom:5px; font-size:0.9rem;">-- Hz</div>
            <div class="inst-row">
                <span style="display:flex; align-items:center;"><span id="mini-{{b[0]}}" class="dot-mini"></span>INS D: <b id="inst-d-{{b[0]}}" style="margin-left:4px;">--</b>%</span>
                <span>INS F: <b id="inst-f-{{b[0]}}" style="margin-left:4px; color:var(--accent-blue);">--</b> Hz</span>
            </div>
            <div class="sv-box">
                <div id="sv-nagan-{{b[0]}}" class="sv-label">NAGAN</div>
                <div id="sv-arun-{{b[0]}}" class="sv-label">ARUN</div>
                <div id="sv-medan-{{b[0]}}" class="sv-label">MEDAN</div>
            </div>
            <div class="sv-box" style="margin-top:2px;">
                <div id="amp-nagan-{{b[0]}}" class="amp-val">-- MW</div>
                <div id="amp-arun-{{b[0]}}" class="amp-val">-- MW</div>
                <div id="amp-medan-{{b[0]}}" class="amp-val">-- MW</div>
            </div>

            {% if b[0] == 'low' %}
            <div class="chart-container">
                <label class="hide-toggle"><input type="checkbox" id="hideAkf"> Hide AKF</label>
                <select id="timeRange" class="time-select" onchange="updateData()">
                    <option value="5">5 Min</option><option value="15">15 Min</option>
                    <option value="30">30 Min</option><option value="60">1 Hour</option>
                </select>
                <canvas id="trendCanvas"></canvas>
            </div>
            {% endif %}
        </div>
        {% endfor %}
    </main>

    <footer>
        Copyright © 2025 PLN UIP3B SUMATERA. All Rights Reserved.
    </footer>

    <script>
        let resizeTimer;
        function checkFullScreen() {
            const bar = document.getElementById('sub-header-bar');
            const isFullScreen = (window.innerHeight >= (screen.height - 15)); 
            
            if (isFullScreen) {
                bar.style.display = 'flex';
                document.body.classList.add('is-fullscreen');
            } else {
                bar.style.display = 'none';
                document.body.classList.remove('is-fullscreen');
            }
            updateData(); 
        }

        window.addEventListener('resize', () => {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(checkFullScreen, 100); 
        });
        
        checkFullScreen();

        const canvas = document.getElementById('trendCanvas');
        const ctx = canvas ? canvas.getContext('2d') : null;
        let audioCtx = null; let isAudioEnabled = false; let alarmInterval = null; let currentStatus = "NORMAL";

        function toggleAudio() {
            if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            isAudioEnabled = !isAudioEnabled;
            const btn = document.getElementById('btnAudio');
            btn.innerText = isAudioEnabled ? "AUDIO ON" : "ENABLE AUDIO";
            btn.className = isAudioEnabled ? "audio-toggle audio-on" : "audio-toggle";
            if(isAudioEnabled && audioCtx.state === 'suspended') audioCtx.resume();
            manageAlarmSound();
        }

        function playBeep(freq, dur, vol, waveType = 'sine') {
            if (!isAudioEnabled || !audioCtx) return;
            const osc = audioCtx.createOscillator(); const gain = audioCtx.createGain();
            osc.type = waveType; osc.frequency.setValueAtTime(freq, audioCtx.currentTime);
            gain.gain.setValueAtTime(vol, audioCtx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + dur);
            osc.connect(gain); gain.connect(audioCtx.destination);
            osc.start(); osc.stop(audioCtx.currentTime + dur);
        }

        function manageAlarmSound() {
            if (alarmInterval) clearInterval(alarmInterval);
            if (!isAudioEnabled) return;
            if (currentStatus === "CRITICAL") alarmInterval = setInterval(() => playBeep(880, 0.15, 0.3, 'sawtooth'), 300);
            else if (currentStatus === "WARNING") alarmInterval = setInterval(() => playBeep(440, 0.4, 0.2, 'sine'), 2000);
        }

        function openModal() { document.getElementById('configModal').style.display = 'block'; }
        function closeModal() { document.getElementById('configModal').style.display = 'none'; }

        function getDampingColor(val) {
            if (val < {{cfg.crit_val}}) return '#f85149'; 
            if (val < {{cfg.warn_val}}) return '#d29922'; 
            return '#3fb950'; 
        }

        function getDampingClass(val) {
            if (val < {{cfg.crit_val}}) return 'd-crit';
            if (val < {{cfg.warn_val}}) return 'd-warn';
            return 'd-ok';
        }

        function drawChart(history, rangeMin) {
            if (!ctx || history.length < 1) return;
            const hideAkf = document.getElementById('hideAkf').checked;
            const dpr = window.devicePixelRatio || 1;
            const rect = canvas.getBoundingClientRect();
            
            canvas.width = rect.width * dpr; 
            canvas.height = rect.height * dpr;
            ctx.resetTransform(); 
            ctx.scale(dpr, dpr);
            
            const w = rect.width, h = rect.height;
            const pL = 35, pB = 30, pT = 35, pR = 15;
            const cW = w - pL - pR, cH = h - pT - pB;
            ctx.clearRect(0, 0, w, h);

            const endTime = new Date(history[history.length - 1].time).getTime();
            const startTime = endTime - (rangeMin * 60 * 1000);
            const getX = (timeStr) => {
                const t = new Date(timeStr).getTime();
                const ratio = (t - startTime) / (endTime - startTime);
                return pL + (ratio * cW);
            };
            const getY = (v) => pT + cH - (Math.min(20, Math.max(0, v)) / 20 * cH);

            ctx.font = '9px Consolas'; ctx.fillStyle = '#8b949e'; ctx.textAlign = 'right';
            [0, 5, 10, 15, 20].forEach(val => {
                let y = getY(val);
                ctx.fillText(val + '%', pL - 5, y + 3);
                ctx.beginPath(); ctx.moveTo(pL, y); ctx.lineTo(w - pR, y);
                ctx.strokeStyle = '#30363d'; ctx.stroke();
            });

            ctx.textAlign = 'center'; ctx.fillStyle = '#58a6ff';
            for(let i=0; i<6; i++) {
                const tickTime = startTime + (i * (endTime - startTime) / 5);
                const timeStr = new Date(tickTime).toLocaleTimeString('id-ID', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
                ctx.fillText(timeStr, pL + (i * cW / 5), h - 8);
            }

            ctx.save(); ctx.beginPath(); ctx.rect(pL, pT, cW, cH); ctx.clip();
            if (!hideAkf) {
                for (let i = 1; i < history.length; i++) {
                    const x1 = getX(history[i-1].time); const x2 = getX(history[i].time);
                    if (x2 < pL) continue;
                    ctx.strokeStyle = getDampingColor(history[i].d_akf); ctx.lineWidth = 2.5;
                    ctx.beginPath(); ctx.moveTo(x1, getY(history[i-1].d_akf)); ctx.lineTo(x2, getY(history[i].d_akf)); ctx.stroke();
                }
            }
            history.forEach(pt => {
                const x = getX(pt.time);
                if (x >= pL) {
                    ctx.fillStyle = getDampingColor(pt.d_raw);
                    ctx.beginPath(); ctx.arc(x, getY(pt.d_raw), 1.2, 0, Math.PI*2); ctx.fill();
                }
            });
            ctx.restore();
        }

        async function updateData() {
            const range = document.getElementById('timeRange')?.value || 5;
            try {
                const res = await fetch(`/api/data?min=${range}`); const data = await res.json();
                requestAnimationFrame(() => {
                    let highestStatus = "NORMAL";
                    ['low', 'mid', 'high'].forEach(band => {
                        const info = data.current[band];
                        if (info) {
                            if (info.status === "CRITICAL") highestStatus = "CRITICAL";
                            else if (info.status === "WARNING" && highestStatus !== "CRITICAL") highestStatus = "WARNING";

                            const card = document.getElementById(`card-${band}`);
                            document.getElementById(`val-${band}`).innerText = info.d_akf.toFixed(2) + "%";
                            document.getElementById(`freq-${band}`).innerText = info.f.toFixed(3) + " Hz";
                            document.getElementById(`inst-d-${band}`).innerText = info.d_raw.toFixed(1);
                            document.getElementById(`inst-f-${band}`).innerText = info.f.toFixed(3);
                            
                            card.classList.remove('d-ok', 'd-warn', 'd-crit', 'active-ok', 'active-warn', 'active-crit');
                            card.classList.add(getDampingClass(info.d_val_check));
                            card.classList.add(info.status === "CRITICAL" ? "active-crit" : (info.status === "WARNING" ? "active-warn" : "active-ok"));

                            const mini = document.getElementById(`mini-${band}`);
                            const dCol = getDampingColor(info.d_raw);
                            mini.style.backgroundColor = dCol; mini.style.boxShadow = `0 0 10px ${dCol}`;
                            
                            ['nagan', 'arun', 'medan'].forEach(loc => {
                                const el = document.getElementById(`sv-${loc}-${band}`);
                                el.className = "sv-label " + (info.sv[loc] === "SOURCE" ? "sv-source" : (info.sv[loc] === "VICTIM" ? "sv-victim" : ""));
                                document.getElementById(`amp-${loc}-${band}`).innerText = info.amps[loc].toFixed(2) + " MW";
                            });
                        }
                    });
                    if (highestStatus !== currentStatus) { currentStatus = highestStatus; manageAlarmSound(); }
                    if (data.history) drawChart(data.history, parseInt(range));
                });
            } catch (e) { console.error(e); }
        }

        setInterval(updateData, 5000);
        setInterval(() => { document.getElementById('sys-clock').innerText = new Date().toLocaleTimeString('id-ID',{hour12:false}); }, 1000);
        updateData();
    </script>
</body>
</html>
"""

# --- 4. BACKEND LOGIC ---
@app.route('/api/data')
def api_data():
    minutes = request.args.get('min', default=5, type=int)
    client = InfluxDBClient(url=INFLUXDB_CONFIG['url'], token=INFLUXDB_CONFIG['token'], org=INFLUXDB_CONFIG['org'])
    q_api = client.query_api()
    current_data = {}; history = []
    global ALARM_SETTINGS
    try:
        q_raw = f'from(bucket: "{INFLUXDB_CONFIG["bucket"]}") |> range(start: -35s) |> filter(fn: (r) => r._measurement == "{INFLUXDB_CONFIG["measurement_opt"]}") |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")'
        q_ssi = f'from(bucket: "{INFLUXDB_CONFIG["bucket"]}") |> range(start: -1m) |> filter(fn: (r) => r._measurement == "{INFLUXDB_CONFIG["measurement_ssi"]}") |> last()'
        res_ssi = q_api.query(q_ssi)
        ssi_map = {r.get_field(): r.get_value() for t in res_ssi for r in t.records}
        res_raw = q_api.query(q_raw)
        p_m, p_n, p_a, f_s = [], [], [], []
        if res_raw:
            for r in res_raw[0].records:
                p_m.append(r.values.get('totw_pmu_1', np.nan))
                p_n.append(r.values.get('totw_pmu_2', np.nan))
                p_a.append(r.values.get('totw_pmu_3', np.nan))
                f_s.append(r.values.get('freq_PMU_1', np.nan))
        for band in ['low', 'mid', 'high']:
            d_akf = float(ssi_map.get(f'{band}_band_d_akf', 10.0))
            d_raw = float(ssi_map.get(f'{band}_band_d', 10.0))
            f_min, f_max = {'low':(0.1, 0.9), 'mid':(0.9, 1.3), 'high':(1.3, 2.5)}[band]
            amps = {
                "medan": calculate_hilbert_amp(np.array(p_m, dtype=float), f_min, f_max),
                "nagan": calculate_hilbert_amp(np.array(p_n, dtype=float), f_min, f_max),
                "arun": calculate_hilbert_amp(np.array(p_a, dtype=float), f_min, f_max)
            }
            mode = ALARM_SETTINGS['logic_mode']
            val_check = d_akf if mode == 'akf' else (d_raw if mode == 'raw' else max(d_akf, d_raw))
            amp_trigger = (amps['medan'] > ALARM_SETTINGS['amp_medan'] or amps['nagan'] > ALARM_SETTINGS['amp_nagan'] or amps['arun'] > ALARM_SETTINGS['amp_arun'])
            st = "NORMAL"
            if val_check < ALARM_SETTINGS['crit_val'] and amp_trigger: st = "CRITICAL"
            elif val_check < ALARM_SETTINGS['warn_val'] and amp_trigger: st = "WARNING"
            current_data[band] = {
                "d_akf": d_akf, "d_raw": d_raw, "d_val_check": val_check,
                "f": float(ssi_map.get(f'{band}_band_f', 0.0)),
                "status": st, "amps": amps,
                "sv": calculate_sv_standard(np.array(p_m), np.array(p_n), np.array(p_a), np.array(f_s), band)
            }
        q_hist = f'''from(bucket: "{INFLUXDB_CONFIG["bucket"]}") |> range(start: -{minutes}m) |> filter(fn: (r) => r._measurement == "{INFLUXDB_CONFIG["measurement_ssi"]}") |> filter(fn: (r) => r._field == "low_band_d_akf" or r._field == "low_band_d") |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value") |> sort(columns: ["_time"])'''
        res_hist = q_api.query(q_hist); raw_history = []
        if res_hist:
            for t in res_hist:
                for r in t.records: raw_history.append({'time': r.get_time().isoformat(), 'd_akf': r.values.get('low_band_d_akf', 10.0), 'd_raw': r.values.get('low_band_d', 10.0)})
        history = lttb_downsample(raw_history, 500) if len(raw_history) > 500 else raw_history
    except Exception as e: print(f"API Error: {e}")
    finally: client.close()
    return jsonify({"current": current_data, "history": history})

@app.route('/save-config', methods=['POST'])
def save_cfg_route():
    global ALARM_SETTINGS
    new_cfg = {'warn_val': float(request.form.get('warn_val')), 'crit_val': float(request.form.get('crit_val')), 'logic_mode': request.form.get('logic_mode'), 'amp_medan': float(request.form.get('amp_medan')), 'amp_nagan': float(request.form.get('amp_nagan')), 'amp_arun': float(request.form.get('amp_arun'))}
    save_config(new_cfg); ALARM_SETTINGS = new_cfg
    return redirect(url_for('index'))

@app.route('/')
def index(): return render_template_string(HTML_TEMPLATE, cfg=ALARM_SETTINGS)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
