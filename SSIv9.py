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

# --- KONFIGURASI INFLUXDB ---
INFLUXDB_CONFIG = {
    'url': 'http://172.17.3.223:8086',
    'token': 'Wo8Rc63Bep01wXaBlM06TgN0TGOvJw4ygVa39dRgcGqwmPs0-aV4aZIv4191xdYJLsjlV2MZvSunX21uU8xITg==',
    'org': 'PLN',
    'source_bucket': 'pmu_synced_raw',
    'source_measurement': 'pmu_synced_opt',
    'source_field_freq1': 'freq_PMU_1', 
    'source_field_freq3': 'freq_PMU_3',
    'dest_bucket': 'pmu_synced_raw',
    'dest_measurement_final_modes': 'pmu_ssi_selected_modes'
}

# --- PARAMETER TEKNIS ---
FS = 25.0              # Sampling rate (Hz)
MAX_BUF = 2500         # Panjang buffer (100 detik data)
WINDOWS = [1500, 2500] # Window size untuk analisis (60s dan 100s)
ORDERS = [10, 20, 30]  # Model orders untuk SSI
MIN_VOTES = 3          # Minimal kandidat dalam satu cluster
STABILITY_TOL = 0.05   # Toleransi frekuensi (Hz) untuk pengelompokan (clustering)
SAFETY_LAG = 3.0       # Delay penarikan data untuk sinkronisasi (detik)

# Batas Damping yang Masuk Akal (Logic Filter)
DAMPING_MAX_THRESHOLD = 20.0 
DAMPING_MIN_THRESHOLD = 0.1

class AdaptiveKalmanFilter:
    """Implementasi Adaptive Kalman Filter untuk meratakan hasil Damping"""
    def __init__(self, q_init=0.0001, r_init=1.5):
        self.q = q_init  # Process noise
        self.r = r_init  # Measurement noise (ditingkatkan agar lebih smooth)
        self.p = 1.0     # Error covariance
        self.x = None    # State estimate
        
    def update(self, z):
        if self.x is None:
            self.x = z
            return self.x
        
        # Prediction
        p_pred = self.p + self.q
        
        # Measurement Update
        innovation = z - self.x
        k_gain = p_pred / (p_pred + self.r)
        
        self.x = self.x + k_gain * innovation
        self.p = (1 - k_gain) * p_pred
        
        # Adaptive adjustment of Q
        if abs(innovation) > 2.0:
            self.q = min(0.01, self.q * 1.1)
        else:
            self.q = max(0.00001, self.q * 0.9)
            
        return float(self.x)

class BandProcessorWithAKF:
    def __init__(self):
        self.buf_f1 = deque(maxlen=MAX_BUF)
        self.buf_f3 = deque(maxlen=MAX_BUF)
        self.ts_buf = deque(maxlen=MAX_BUF)
        self.filter_coeffs = butter(4, [0.1, 2.5], btype='band', fs=FS)
        self.last_sent_time = None
        
        # Inisialisasi AKF untuk masing-masing Band
        self.akf_low = AdaptiveKalmanFilter()
        self.akf_high = AdaptiveKalmanFilter()

    def compute_ssi(self, y1, y3, n):
        """Covariance-driven SSI dengan MIMO Normalization"""
        try:
            # 1. Normalisasi MIMO (Zero mean + Std Dev scaling)
            y1_n = (y1 - np.mean(y1)) / (np.std(y1) + 1e-9)
            y3_n = (y3 - np.mean(y3)) / (np.std(y3) + 1e-9)
            
            Y = np.vstack([y1_n, y3_n])
            dt = 1/FS
            
            # 2. Toeplitz / Hankel Matrix construction (Covariance-driven)
            R = [(Y[:, i:] @ Y[:, :-i].T) / (Y.shape[1] - i) if i > 0 else (Y @ Y.T) / Y.shape[1] for i in range(n * 2)]
            H = np.zeros((n * 2, n * 2)) 
            for i in range(n):
                for j in range(n): H[i*2:(i+1)*2, j*2:(j+1)*2] = R[i+j+1]
            
            # 3. SVD dan Estimasi Matriks A
            U, S, _ = la.svd(H, full_matrices=False)
            Obs = U[:, :n] @ la.sqrtm(np.diag(S[:n]))
            A = la.lstsq(Obs[:-2, :], Obs[2:, :])[0]
            eigvals = la.eigvals(A)
            
            res = []
            for ev in eigvals:
                s_pole = np.log(ev) / dt
                f = np.abs(np.imag(s_pole)) / (2 * np.pi)
                d = (-np.real(s_pole) / np.abs(s_pole)) * 100
                if 0.1 <= f <= 2.5: res.append((f, d))
            return res
        except Exception: return []

    def get_best_mode(self, candidates, f_min, f_max):
        """Clustering menggunakan Median untuk menolak Outlier"""
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
                    cluster_f.append(f)
                    cluster_d.append(d)
                else:
                    rem.append((f, d))
            band_cands = rem
            
            # Output menggunakan MEDIAN agar lebih stabil
            clusters.append({
                'f': np.median(cluster_f), 
                'd': np.median(cluster_d), 
                'v': len(cluster_f)
            })
        
        valid = [c for c in clusters if c['v'] >= MIN_VOTES]
        return max(valid, key=lambda x: x['v']) if valid else None

    def process(self, write_api):
        if len(self.ts_buf) < MAX_BUF:
            logging.info(f"--- [BUFFERING] {len(self.ts_buf)}/{MAX_BUF} ---")
            return

        ts_now = self.ts_buf[-1]
        # Kirim data setiap ~5 detik
        if self.last_sent_time and (ts_now - self.last_sent_time).total_seconds() < 4.9:
            return

        all_cands = []
        # Bandpass Filter sebelum SSI
        y1_raw = np.array(list(self.buf_f1))
        y3_raw = np.array(list(self.buf_f3))
        
        for w in WINDOWS:
            y1 = filtfilt(self.filter_coeffs[0], self.filter_coeffs[1], y1_raw[-w:])
            y3 = filtfilt(self.filter_coeffs[0], self.filter_coeffs[1], y3_raw[-w:])
            for n in ORDERS:
                all_cands.extend(self.compute_ssi(y1, y3, n))

        mode_low = self.get_best_mode(all_cands, 0.1, 1.0)
        mode_high = self.get_best_mode(all_cands, 1.0, 2.0)

        p = Point(INFLUXDB_CONFIG['dest_measurement_final_modes']).time(ts_now)
        has_data = False
        
        # LOGIKA FILTER & UPDATE AKF
        if mode_low:
            if DAMPING_MIN_THRESHOLD <= mode_low['d'] <= DAMPING_MAX_THRESHOLD:
                d_akf = self.akf_low.update(mode_low['d'])
                p.field("low_band_f", float(round(mode_low['f'], 3)))
                p.field("low_band_d", float(round(mode_low['d'], 2)))
                p.field("low_band_d_akf", float(round(d_akf, 2)))
                has_data = True

        if mode_high:
            if DAMPING_MIN_THRESHOLD <= mode_high['d'] <= DAMPING_MAX_THRESHOLD:
                d_akf = self.akf_high.update(mode_high['d'])
                p.field("high_band_f", float(round(mode_high['f'], 3)))
                p.field("high_band_d", float(round(mode_high['d'], 2)))
                p.field("high_band_d_akf", float(round(d_akf, 2)))
                has_data = True

        if has_data:
            write_api.write(bucket=INFLUXDB_CONFIG['dest_bucket'], record=p)
            logging.info(f"--- [UPDATE] L: {mode_low['f'] if mode_low else '-'}Hz, D: {mode_low['d'] if mode_low else '-'}% | H: {mode_high['f'] if mode_high else '-'}Hz ---")
            self.last_sent_time = ts_now

def main():
    logging.info("--- Memulai SSI v2 (MIMO + Median + Logic Filter Enabled) ---")
    client = InfluxDBClient(url=INFLUXDB_CONFIG['url'], token=INFLUXDB_CONFIG['token'], org=INFLUXDB_CONFIG['org'])
    q_api = client.query_api()
    w_api = client.write_api(write_options=SYNCHRONOUS)
    
    proc = BandProcessorWithAKF()
    last_ts = datetime.now(timezone.utc) - timedelta(seconds=15)

    while True:
        now = datetime.now(timezone.utc)
        query = f'''from(bucket: "{INFLUXDB_CONFIG['source_bucket']}") 
                    |> range(start: {last_ts.isoformat()}, stop: {(now - timedelta(seconds=SAFETY_LAG)).isoformat()})
                    |> filter(fn: (r) => r._measurement == "{INFLUXDB_CONFIG['source_measurement']}")
                    |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
                    |> sort(columns: ["_time"])'''
        try:
            records = [r for t in q_api.query(query) for r in t.records]
            if records:
                for r in records:
                    proc.buf_f1.append(float(r[INFLUXDB_CONFIG['source_field_freq1']]))
                    proc.buf_f3.append(float(r[INFLUXDB_CONFIG['source_field_freq3']]))
                    proc.ts_buf.append(r.get_time())
                
                proc.process(w_api)
                last_ts = records[-1].get_time() + timedelta(microseconds=1)
            else:
                logging.debug("No new records...")
        except Exception as e:
            logging.error(f"Error Loop: {e}")
            
        time.sleep(2.0)

if __name__ == '__main__':
    main()
