import time
import logging
import numpy as np
import scipy.linalg as la
from datetime import datetime, timezone, timedelta
from collections import deque
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from scipy.signal import butter, filtfilt

# --- KONFIGURASI LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- KONFIGURASI INFLUXDB & PMU ---
PMU_LIST = ['PMU_1', 'PMU_2', 'PMU_3']

INFLUXDB_CONFIG = {
    'url': 'http://172.17.3.223:8086',
    'token': 'Wo8Rc63Bep01wXaBlM06TgN0TGOvJw4ygVa39dRgcGqwmPs0-aV4aZIv4191xdYJLsjlV2MZvSunX21uU8xITg==',
    'org': 'PLN',
    'source_bucket': 'pmu_synced_raw',
    'source_measurement': 'pmu_synced_opt',
    'dest_bucket': 'pmu_synced_raw',
    'dest_measurement_final_modes': 'pmu_ssi_selected_modes'
}

# --- PARAMETER TEKNIS (TETAP SAMA) ---
FS = 25.0              
MAX_BUF = 2500         
WINDOWS = [1500, 2500] 
ORDERS = [10, 20, 30]  
MIN_VOTES = 3          
STABILITY_TOL = 0.05   
SAFETY_LAG = 2.0       
MAX_NAN_TOLERANCE = 0.05 

class AdaptiveKalmanFilter:
    def __init__(self, q_init=0.0001, r_init=1.0):
        self.q, self.r, self.p, self.x = q_init, r_init, 1.0, None
        
    def update(self, z):
        if self.x is None: self.x = z; return self.x
        p_pred = self.p + self.q
        innovation = z - self.x
        k_gain = p_pred / (p_pred + self.r)
        self.x += k_gain * innovation
        self.p = (1 - k_gain) * p_pred
        self.q = min(0.01, self.q * 1.1) if abs(innovation) > 2.0 else max(0.00001, self.q * 0.9)
        return float(self.x)

class AdaptiveSSIProcessor:
    def __init__(self, pmu_names):
        self.pmu_names = pmu_names
        self.buffers = {name: deque(maxlen=MAX_BUF) for name in pmu_names}
        self.ts_buf = deque(maxlen=MAX_BUF)
        self.filter_coeffs = butter(4, [0.1, 2.0], btype='band', fs=FS)
        self.akf_low = AdaptiveKalmanFilter()
        self.akf_mid = AdaptiveKalmanFilter() 
        self.akf_high = AdaptiveKalmanFilter()

    def interpolate_signal(self, sig):
        mask = np.isnan(sig)
        nan_count = np.sum(mask)
        if nan_count == 0: return sig, True
        if nan_count > (len(sig) * MAX_NAN_TOLERANCE): return sig, False
        try:
            indices = np.arange(len(sig))
            sig[mask] = np.interp(indices[mask], indices[~mask], sig[~mask])
            return sig, True
        except: return sig, False

    def compute_ssi(self, signals, n):
        try:
            normalized = [(s - np.mean(s)) / (np.std(s) + 1e-9) for s in signals]
            Y = np.vstack(normalized)
            ch, N = Y.shape
            dt = 1/FS
            R = [(Y[:, i:] @ Y[:, :-i].T) / (N - i) if i > 0 else (Y @ Y.T) / N for i in range(n * 2)]
            H = np.zeros((n * ch, n * ch))
            for i in range(n):
                for j in range(n): H[i*ch:(i+1)*ch, j*ch:(j+1)*ch] = R[i+j+1]
            U, S, _ = la.svd(H, full_matrices=False)
            Obs = U[:, :n] @ la.sqrtm(np.diag(S[:n]))
            A = la.lstsq(Obs[:-ch, :], Obs[ch:, :])[0]
            eigvals = la.eigvals(A)
            res = []
            for ev in eigvals:
                s_pole = np.log(ev + 0j) / dt
                f = np.abs(np.imag(s_pole)) / (2 * np.pi)
                d = (-np.real(s_pole) / np.abs(s_pole)) * 100
                if 0.1 <= f <= 2.5: res.append((f, d))
            return res
        except: return []

    def get_best_mode(self, candidates, f_min, f_max):
        band_cands = [c for c in candidates if f_min <= c[0] < f_max]
        if not band_cands: return None
        clusters = []
        band_cands.sort(key=lambda x: x[0])
        while band_cands:
            base_f, base_d = band_cands.pop(0)
            cluster_f, cluster_d = [base_f], [base_d]
            rem = []
            for f, d in band_cands:
                if abs(f - base_f) <= STABILITY_TOL:
                    cluster_f.append(f); cluster_d.append(d)
                else: rem.append((f, d))
            band_cands = rem
            clusters.append({'f': np.median(cluster_f), 'd': np.median(cluster_d), 'v': len(cluster_f)})
        valid = [c for c in clusters if c['v'] >= MIN_VOTES]
        return max(valid, key=lambda x: x['v']) if valid else None

    def process(self, write_api):
        if len(self.ts_buf) < MAX_BUF: return False
        ts_now = self.ts_buf[-1]
        active_signals, active_names = [], []
        for name in self.pmu_names:
            raw_sig = np.array(list(self.buffers[name]), dtype=float)
            clean_sig, is_valid = self.interpolate_signal(raw_sig)
            if is_valid:
                active_signals.append(clean_sig)
                active_names.append(name)

        if not active_signals: return False

        all_cands = []
        for w in WINDOWS:
            try:
                filtered = [filtfilt(self.filter_coeffs[0], self.filter_coeffs[1], s[-w:]) for s in active_signals]
                for n in ORDERS: all_cands.extend(self.compute_ssi(filtered, n))
            except: continue

        mode_low = self.get_best_mode(all_cands, 0.1, 0.9)
        mode_mid = self.get_best_mode(all_cands, 0.9, 1.3)
        mode_high = self.get_best_mode(all_cands, 1.3, 2.1)

        p = Point(INFLUXDB_CONFIG['dest_measurement_final_modes']).time(ts_now)
        p.tag("pmu_sources", ",".join(active_names))
        
        has_data = False
        for m, akf, pre in [(mode_low, self.akf_low, "low"), (mode_mid, self.akf_mid, "mid"), (mode_high, self.akf_high, "high")]:
            if m and 0.1 <= m['d'] <= 25.0:
                d_filtered = akf.update(m['d'])
                p.field(f"{pre}_band_f", float(round(m['f'], 3)))
                p.field(f"{pre}_band_d", float(round(m['d'], 2)))
                p.field(f"{pre}_band_d_akf", float(round(d_filtered, 2)))
                has_data = True

        if has_data:
            write_api.write(bucket=INFLUXDB_CONFIG['dest_bucket'], record=p)
            return True
        return False

def run_main():
    client = InfluxDBClient(url=INFLUXDB_CONFIG['url'], token=INFLUXDB_CONFIG['token'], org=INFLUXDB_CONFIG['org'])
    q_api, w_api = client.query_api(), client.write_api(write_options=SYNCHRONOUS)
    proc = AdaptiveSSIProcessor(PMU_LIST)
    
    # KODE AWAL: Mulai dari sekarang (Menunggu buffer isi alami)
    last_ts = datetime.now(timezone.utc) - timedelta(seconds=SAFETY_LAG)
    last_activity = time.time()
    
    # Tambahan 1 baris variabel untuk counter catch-up
    new_data_count = 0 
    
    logging.info("--- Sistem SSI Dimulai (Menunggu Buffer Penuh...) ---")

    try:
        while True:
            now = datetime.now(timezone.utc)
            query = f'''from(bucket: "{INFLUXDB_CONFIG['source_bucket']}") 
                |> range(start: {last_ts.isoformat()}, stop: {(now - timedelta(seconds=SAFETY_LAG)).isoformat()})
                |> filter(fn: (r) => r._measurement == "{INFLUXDB_CONFIG['source_measurement']}")
                |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
                |> sort(columns: ["_time"])'''
            
            try:
                tables = q_api.query(query)
                for table in tables:
                    for r in table.records:
                        found_any = False
                        for name in PMU_LIST:
                            val = r.values.get(f"freq_{name}")
                            if val is not None and float(val) > 40.0:
                                proc.buffers[name].append(float(val))
                                found_any = True
                            else:
                                proc.buffers[name].append(np.nan)
                        
                        if found_any:
                            proc.ts_buf.append(r.get_time())
                            last_ts = r.get_time() + timedelta(microseconds=1)
                            
                            # LOGIKA CATCH-UP (PERUBAHAN UTAMA):
                            # Jika buffer penuh, hitung SSI setiap ada 25 data baru masuk.
                            # Diletakkan di dalam loop record agar mengejar ketertinggalan dengan cepat.
                            if len(proc.ts_buf) >= MAX_BUF:
                                new_data_count += 1
                                if new_data_count >= 25: 
                                    proc.process(w_api)
                                    new_data_count = 0
                
                last_activity = time.time()

            except Exception as e:
                logging.error(f"Query Error: {e}")

            if time.time() - last_activity > 60:
                logging.warning("Data Source Macet...")
                break
                
            print(f"\r[STATUS] Buff: {len(proc.ts_buf)}/{MAX_BUF} ", end="")
            time.sleep(1.0) 

    finally: client.close()

if __name__ == '__main__':
    while True:
        try: run_main()
        except KeyboardInterrupt: break
        except Exception: time.sleep(2)
