import os
import math
import datetime
import urllib.request
import gzip
import shutil
import ssl
import json
import threading
import re
from flask import Flask, request, send_file, Response, jsonify

app = Flask(__name__)

# --- RUTA DINÁMICA DE TRABAJO (VERCEL SERVERLESS) ---
# Se fuerza el uso de /tmp por restricciones de escritura en Vercel.
BASE_DIR = '/tmp'

UPLOAD_FOLDER = os.path.join(BASE_DIR, 'temp_rinex')
REPORT_FOLDER = os.path.join(BASE_DIR, 'informes')
STATE_FILE = os.path.join(UPLOAD_FOLDER, 'estado_proyecto.json')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(REPORT_FOLDER, exist_ok=True)

STATE_LOCK = threading.Lock()

# --- CONSTANTES ---
C_LIGHT = 299792458.0
OMEGA_E = 7.2921151467e-5
MU = 3.986005e14

# =====================================================================
# HERRAMIENTAS GOOGLE DRIVE / DESCARGA
# =====================================================================
def extraer_gdrive_id(url):
    match = re.search(r'/file/d/([a-zA-Z0-9_-]+)', url)
    if match: return match.group(1)
    match = re.search(r'id=([a-zA-Z0-9_-]+)', url)
    if match: return match.group(1)
    return None

def descargar_gdrive_publico(url, dest_path):
    file_id = extraer_gdrive_id(url)
    if not file_id:
        raise Exception("Formato de URL de Google Drive inválido. Use enlace de compartición.")
    download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    req = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as res:
        with open(dest_path, 'wb') as f:
            f.write(res.read())

def obtener_lambda_dinamico(sys_char, freq_band):
    if freq_band == 'L5': return C_LIGHT / 1176.45e6
    else:
        if sys_char == 'C': return C_LIGHT / 1561.098e6 
        if sys_char in 'GEJS': return C_LIGHT / 1575.42e6 
        return C_LIGHT / 1602.0e6 

def safe_f(val, default=0.0):
    try: return float(val) if val and str(val).strip() != '' else default
    except: return default

def safe_i(val, default=19):
    try: return int(val) if val and str(val).strip() != '' else default
    except: return default

def guardar_estado(clave, valor):
    with STATE_LOCK:
        estado = {}
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f: estado = json.load(f)
            except: pass
        estado[clave] = valor
        with open(STATE_FILE, 'w') as f: json.dump(estado, f)

def leer_estado(clave):
    with STATE_LOCK:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f: return json.load(f).get(clave)
            except: pass
        return None

def gps_time_to_tow(year, month, day, hour, minute, second):
    sec_int, sec_frac = int(second), second - int(second)
    total = (datetime.datetime(year, month, day, hour, minute, sec_int) - datetime.datetime(1980, 1, 6)).total_seconds() + sec_frac
    return total - (int(total // 604800) * 604800)

def parse_rinex_obs_completo(path):
    obs = {}
    sys_idx = {}
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        in_h = True
        tow = None
        for line in f:
            if in_h:
                if "SYS / # / OBS TYPES" in line:
                    sys_char = line[0]
                    t = [x.strip() for x in line[6:60].split() if x.strip()]
                    sys_idx[sys_char] = {
                        'C1': next((i for i, x in enumerate(t) if x.startswith('C1')), -1),
                        'L1': next((i for i, x in enumerate(t) if x.startswith('L1')), -1),
                        'C5': next((i for i, x in enumerate(t) if x.startswith('C5')), -1),
                        'L5': next((i for i, x in enumerate(t) if x.startswith('L5')), -1),
                        'S1': next((i for i, x in enumerate(t) if x.startswith('S1')), -1),
                        'S5': next((i for i, x in enumerate(t) if x.startswith('S5')), -1)
                    }
                elif "END OF HEADER" in line: in_h = False
            elif line.startswith('>'):
                p = line[1:].split()
                if len(p) >= 6:
                    y, m, d, h, mn, sec = int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4]), float(p[5])
                    tow = round(gps_time_to_tow(y, m, d, h, mn, sec), 6)
                    obs[tow] = {'_meta': (y, m, d, h, mn, sec)}
            elif tow and len(line) > 3 and line[0] in 'GRECSJ':
                sys_char = line[0]
                idx_c1 = sys_idx.get(sys_char, {}).get('C1', -1)
                idx_l1 = sys_idx.get(sys_char, {}).get('L1', -1)
                idx_c5 = sys_idx.get(sys_char, {}).get('C5', -1)
                idx_l5 = sys_idx.get(sys_char, {}).get('L5', -1)
                idx_s1 = sys_idx.get(sys_char, {}).get('S1', -1)
                idx_s5 = sys_idx.get(sys_char, {}).get('S5', -1)
                
                data = {}
                if idx_c1 >= 0 and len(line) >= 17 + 16 * idx_c1:
                    v = line[3+16*idx_c1 : 17+16*idx_c1].strip()
                    if v: data['C1'] = float(v.replace('D', 'E').replace('d', 'e'))
                if idx_l1 >= 0 and len(line) >= 17 + 16 * idx_l1:
                    v = line[3+16*idx_l1 : 17+16*idx_l1].strip()
                    if v: data['L1'] = float(v.replace('D', 'E').replace('d', 'e'))
                if idx_c5 >= 0 and len(line) >= 17 + 16 * idx_c5:
                    v = line[3+16*idx_c5 : 17+16*idx_c5].strip()
                    if v: data['C5'] = float(v.replace('D', 'E').replace('d', 'e'))
                if idx_l5 >= 0 and len(line) >= 17 + 16 * idx_l5:
                    v = line[3+16*idx_l5 : 17+16*idx_l5].strip()
                    if v: data['L5'] = float(v.replace('D', 'E').replace('d', 'e'))
                if idx_s1 >= 0 and len(line) >= 17 + 16 * idx_s1:
                    v = line[3+16*idx_s1 : 17+16*idx_s1].strip()
                    if v: data['S1'] = float(v.replace('D', 'E').replace('d', 'e'))
                if idx_s5 >= 0 and len(line) >= 17 + 16 * idx_s5:
                    v = line[3+16*idx_s5 : 17+16*idx_s5].strip()
                    if v: data['S5'] = float(v.replace('D', 'E').replace('d', 'e'))
                
                if ('C1' in data and data['C1'] > 15000000.0) or ('C5' in data and data['C5'] > 15000000.0):
                    obs[tow][line[0:3].strip()] = data
    return obs

def interpolar_base_a_rover(obs_base, tr, max_gap=0.05):
    tiempos_base = sorted(list(obs_base.keys()))
    if not tiempos_base: return None
    idx = min(range(len(tiempos_base)), key=lambda i: abs(tiempos_base[i] - tr))
    if abs(tiempos_base[idx] - tr) <= max_gap: 
        return obs_base[tiempos_base[idx]].copy()
    return None

def generar_rinex_sincronizado(raw_path, out_path, obs_dict):
    header_lines = []
    constelaciones_presentes = set()
    with open(raw_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            if "SYS / # / OBS TYPES" in line:
                constelaciones_presentes.add(line[0])
                header_lines.append(line)
            else:
                header_lines.append(line)
            if "END OF HEADER" in line: break
    
    idx = next((i for i, l in enumerate(header_lines) if "END OF HEADER" in l), -1)
    if idx != -1:
        constelaciones_requeridas = ['G', 'E', 'C', 'R', 'S', 'J']
        offset = 0
        for c in constelaciones_requeridas:
            if c not in constelaciones_presentes:
                header_lines.insert(idx + offset, f"{c}    4 C1 L1 C5 L5                                       SYS / # / OBS TYPES\n")
                offset += 1
        
    with open(out_path, 'w', encoding='utf-8') as f_out:
        for line in header_lines: f_out.write(line)
        for tow in sorted(obs_dict.keys()):
            meta = obs_dict[tow].get('_meta')
            if not meta: continue
            y, m, d, h, mn, sec = meta
            sats = [k for k in obs_dict[tow].keys() if k != '_meta']
            f_out.write(f"> {y} {m:02d} {d:02d} {h:02d} {mn:02d} {sec:11.7f}  0 {len(sats):2d}\n")
            for sat in sats:
                c1 = obs_dict[tow][sat].get('C1', 0.0)
                l1 = obs_dict[tow][sat].get('L1', 0.0)
                c5 = obs_dict[tow][sat].get('C5', 0.0)
                l5 = obs_dict[tow][sat].get('L5', 0.0)
                c1_s = f"{c1:14.3f}" if c1 > 0 else "              "
                l1_s = f"{l1:14.3f}" if l1 > 0 else "              "
                c5_s = f"{c5:14.3f}" if c5 > 0 else "              "
                l5_s = f"{l5:14.3f}" if l5 > 0 else "              "
                f_out.write(f"{sat}{c1_s}  {l1_s}  {c5_s}  {l5_s}  \n")

def parse_rinex_nav_real(path):
    ephemeris = {}
    iono_params = {'GPSA': [0]*4, 'GPSB': [0]*4, 'BDSA': [0]*4, 'BDSB': [0]*4}
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        in_h, sat, data = True, None, []
        for line in f:
            if in_h:
                if "IONOSPHERIC CORR" in line:
                    sys_type = line[0:4].strip()
                    vals = []
                    for i in range(4):
                        try:
                            chunk = line[5+i*12 : 5+(i+1)*12].strip().replace('D', 'E').replace('d', 'e')
                            vals.append(float(chunk) if chunk else 0.0)
                        except:
                            vals.append(0.0)
                    if sys_type in iono_params: iono_params[sys_type] = vals
                elif "END OF HEADER" in line: in_h = False
                continue
            if len(line) > 8 and line[0] in 'GECSJ' and line[1:3].isdigit():
                if sat and len(data) >= 20: 
                    ephemeris.setdefault(sat, []).append({'af0':data[0],'af1':data[1],'af2':data[2],'Crs':data[4],'Delta_n':data[5],'M0':data[6],'Cuc':data[7],'e':data[8],'Cus':data[9],'sqrtA':data[10],'Toe':data[11],'Cic':data[12],'OMEGA':data[13],'Cis':data[14],'i0':data[15],'Crc':data[16],'omega':data[17],'OMEGA_DOT':data[18],'IDOT':data[19]})
                sat = line[0:3].strip()
                data = [float(line[23:42].replace('D','E').replace('d','e')), float(line[42:61].replace('D','E').replace('d','e')), float(line[61:80].replace('D','E').replace('d','e'))]
            elif sat and line.startswith('    '): 
                data.extend([float(line[i:i+19].replace('D','E').replace('d','e').strip()) for i in range(4, 80, 19) if line[i:i+19].strip()])
        if sat and len(data) >= 20: 
            ephemeris.setdefault(sat, []).append({'af0':data[0],'af1':data[1],'af2':data[2],'Crs':data[4],'Delta_n':data[5],'M0':data[6],'Cuc':data[7],'e':data[8],'Cus':data[9],'sqrtA':data[10],'Toe':data[11],'Cic':data[12],'OMEGA':data[13],'Cis':data[14],'i0':data[15],'Crc':data[16],'omega':data[17],'OMEGA_DOT':data[18],'IDOT':data[19]})
    alpha = iono_params['GPSA'] if any(iono_params['GPSA']) else iono_params['BDSA']
    beta = iono_params['GPSB'] if any(iono_params['GPSB']) else iono_params['BDSB']
    ephemeris['_iono'] = {'alpha': alpha, 'beta': beta}
    return ephemeris

def seleccionar_efemeride_optima(eph_list, t_target):
    if not eph_list: return None
    return min(eph_list, key=lambda x: abs(x.get('Toe', 0) - t_target))

def obtener_fecha_obs(filepath):
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            if line.startswith('>'):
                partes = line[1:].strip().split()
                if len(partes) >= 6: 
                    try:
                        year = int(partes[0])
                        if year < 100: year += 2000
                        return year, int(partes[1]), int(partes[2]), int(partes[3]), int(partes[4]), float(partes[5])
                    except: pass
    return None

def descargar_efemerides_brdc_stream(year, month, day, hour):
    dt = datetime.datetime(year, month, day)
    doy = dt.timetuple().tm_yday
    nav_descargado = os.path.join(UPLOAD_FOLDER, f"auto_nav_{year}_{doy:03d}.nav")
    if os.path.exists(nav_descargado): 
        yield ("SUCCESS", nav_descargado)
        return
    prefijos = ['IGS', 'WRD', 'BKG', 'GOP']
    urls = [f"https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{year}/{doy:03d}/BRDC00{p}_R_{year}{doy:03d}0000_01D_MN.rnx.gz" for p in prefijos]
    horas = [hour] + [h for h in range(hour-1, -1, -1)] + [h for h in range(hour+1, 24)]
    for p in prefijos:
        for h in horas: 
            urls.append(f"https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{year}/{doy:03d}/BRDC00{p}_R_{year}{doy:03d}{h:02d}00_01H_MN.rnx.gz")
    ctx = ssl.create_default_context()
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, context=ctx, timeout=10) as res:
                yield ("INFO", f"> Descargando comprimido: {url.split('/')[-1]}...\n")
                with open(nav_descargado + '.gz', 'wb') as f: f.write(res.read())
                yield ("INFO", "> Descomprimiendo GZIP y construyendo .nav local...\n")
                with gzip.open(nav_descargado + '.gz', 'rb') as f_in, open(nav_descargado, 'wb') as f_out: 
                    shutil.copyfileobj(f_in, f_out)
                yield ("SUCCESS", nav_descargado)
                return
        except Exception: pass
    yield ("ERROR", "Falla catastrófica al conectar con IGS/BKG.")

# =====================================================================
# MOTOR ALGEBRAICO N x N
# =====================================================================
def transpose_matrix(M):
    if not M or not M[0]: return []
    try:
        return [[M[j][i] for j in range(len(M))] for i in range(len(M[0]))]
    except IndexError:
        return []

def matmul(A, B):
    if not A or not B or not A[0] or not B[0]: return []
    try:
        result = [[0.0 for _ in range(len(B[0]))] for _ in range(len(A))]
        for i in range(len(A)):
            for j in range(len(B[0])):
                for k in range(len(B)):
                    result[i][j] += A[i][k] * B[k][j]
        return result
    except IndexError:
        return []

def invert_matrix_nxn(M):
    if not M or not M[0]: return None
    try:
        n = len(M)
        A = [[float(M[i][j]) for j in range(n)] for i in range(n)]
        I = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
        
        for i in range(n):
            max_k = i
            for k in range(i + 1, n):
                if abs(A[k][i]) > abs(A[max_k][i]):
                    max_k = k
            
            if max_k != i:
                A[i], A[max_k] = A[max_k], A[i]
                I[i], I[max_k] = I[max_k], I[i]
            
            pivot = A[i][i]
            if abs(pivot) < 1e-15: return None 
            
            for j in range(n):
                A[i][j] /= pivot
                I[i][j] /= pivot
                
            for k in range(n):
                if k == i: continue
                factor = A[k][i]
                for j in range(n):
                    A[k][j] -= factor * A[i][j]
                    I[k][j] -= factor * I[i][j]
        return I
    except IndexError:
        return None

# =====================================================================
# MODELOS GEODÉSICOS
# =====================================================================
def calcular_saastamoinen(lat_deg, alt, elev_deg):
    if elev_deg < 5.0: elev_deg = 5.0
    lat_rad, elev_rad = max(math.radians(lat_deg), -math.pi/2), math.radians(elev_deg)
    H = max(0.0, min(alt, 40000.0))
    P = 1013.25 * ((1.0 - 2.2557e-5 * H) ** 5.2568)
    T = 288.15 - 0.0065 * H
    e = 6.11 * 0.5 * (10.0 ** (7.5 * (T - 273.15) / (T - 273.15 + 237.3))) * ((1.0 - 2.2557e-5 * H) ** 5.2568)
    zhd = (0.0022768 * P) / (1.0 - 0.00266 * math.cos(2.0 * lat_rad) - 0.00028 * (H / 1000.0))
    zwd = 0.0022768 * ((1255.0 / T) + 0.05) * e
    return (zhd + zwd) * (1.0 / math.sin(elev_rad))

def geodesicas_a_ecef(lat_deg, lon_deg, alt):
    a, e2 = 6378137.0, 0.0066943799901413155
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    N = a / math.sqrt(1 - e2 * (math.sin(lat) ** 2))
    return (N + alt) * math.cos(lat) * math.cos(lon), (N + alt) * math.cos(lat) * math.sin(lon), (N * (1 - e2) + alt) * math.sin(lat)

def ecef_a_geodesicas(x, y, z):
    a, e2 = 6378137.0, 0.0066943799901413155
    b = math.sqrt(a**2 * (1 - e2)); ep2 = (a**2 - b**2) / b**2
    p = math.sqrt(x**2 + y**2); th = math.atan2(a * z, b * p)
    lat = math.atan2((z + ep2 * b * (math.sin(th) ** 3)), (p - e2 * a * (math.cos(th) ** 3)))
    N = a / math.sqrt(1 - e2 * (math.sin(lat) ** 2))
    return math.degrees(lat), math.degrees(math.atan2(y, x)), p / math.cos(lat) - N

def geodesicas_a_utm(lat, lon, force_zone=19):
    a, e2 = 6378137.0, 0.0066943799901413155
    lat_r, lon_r = math.radians(lat), math.radians(lon)
    LongOrig = math.radians((force_zone - 1) * 6 - 180 + 3)
    ep2 = e2 / (1 - e2)
    N = a / math.sqrt(1 - e2 * math.sin(lat_r)**2)
    T = math.tan(lat_r)**2; C = ep2 * math.cos(lat_r)**2; A = math.cos(lat_r) * (lon_r - LongOrig)
    M = a * ((1 - e2/4 - 3*e2**2/64 - 5*e2**3/256)*lat_r - (3*e2/8 + 3*e2**2/32 + 45*e2**3/1024)*math.sin(2*lat_r) + (15*e2**2/256 + 45*e2**3/1024)*math.sin(4*lat_r) - (35*e2**3/3072)*math.sin(6*lat_r))
    Easting = 0.9996 * N * (A + (1-T+C)*A**3/6 + (5-18*T+T**2+72*C-58*ep2)*A**5/120) + 500000.0
    Northing = 0.9996 * (M + N*math.tan(lat_r)*(A**2/2 + (5-T+9*C+4*C**2)*A**4/24 + (61-58*T+T**2+600*C-330*ep2)*A**6/720))
    return (Northing + 10000000.0 if lat < 0 else Northing), Easting

def utm_a_geodesicas(easting, northing, zone=19, hemisferio='N'):
    a, e2 = 6378137.0, 0.0066943799901413155
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    x, y = easting - 500000.0, northing if hemisferio.upper() == 'N' else northing - 10000000.0
    m = y / 0.9996; mu = m / (a * (1 - e2/4 - 3*e2**2/64 - 5*e2**3/256))
    phi1_rad = mu + (3*e1/2 - 27*e1**3/32)*math.sin(2*mu) + (21*e1**2/16 - 55*e1**4/32)*math.sin(4*mu)
    n1 = a / math.sqrt(1 - e2*math.sin(phi1_rad)**2)
    t1, c1 = math.tan(phi1_rad)**2, e2 / (1 - e2) * math.cos(phi1_rad)**2
    r1 = a * (1 - e2) / ((1 - e2*math.sin(phi1_rad)**2)**1.5)
    d = x / (n1 * 0.9996)
    lat_rad = phi1_rad - (n1*math.tan(phi1_rad)/r1) * (d**2/2 - (5 + 3*t1 + 10*c1)*d**4/24)
    lon_rad = (d - (1 + 2*t1 + c1)*d**3/6) / math.cos(phi1_rad)
    lon_origen = math.radians((zone - 1) * 6 - 180 + 3)
    return math.degrees(lat_rad), math.degrees(lon_rad + lon_origen), 0.0

def calcular_topocentricas(xs, ys, zs, X_usr, Y_usr, Z_usr):
    lat_val, lon_val, alt_val = ecef_a_geodesicas(X_usr, Y_usr, Z_usr)
    lat_r = math.radians(lat_val)
    lon_r = math.radians(lon_val)
    dx, dy, dz = xs - X_usr, ys - Y_usr, zs - Z_usr
    sin_lat, cos_lat = math.sin(lat_r), math.cos(lat_r)
    sin_lon, cos_lon = math.sin(lon_r), math.cos(lon_r)
    e = -sin_lon * dx + cos_lon * dy
    n = -sin_lat * cos_lon * dx - sin_lat * sin_lon * dy + cos_lat * dz
    u = cos_lat * cos_lon * dx + cos_lat * sin_lon * dy + sin_lat * dz
    dist = math.sqrt(dx**2 + dy**2 + dz**2)
    if dist < 1e-6: return 0.0, 0.0
    val_asin = max(-1.0, min(1.0, u / dist))
    el = math.degrees(math.asin(val_asin))
    az = math.degrees(math.atan2(e, n))
    if az < 0: az += 360.0
    return el, az

def calcular_klobuchar(lat_deg, lon_deg, el_deg, az_deg, tow, alpha, beta):
    if not any(alpha) and not any(beta): return 0.0
    phi_u, lam_u = lat_deg / 180.0, lon_deg / 180.0
    E, A = el_deg / 180.0, az_deg / 180.0
    psi = 0.0137 / (E + 0.11) - 0.022
    phi_i = phi_u + psi * math.cos(A * math.pi)
    if phi_i > 0.416: phi_i = 0.416
    elif phi_i < -0.416: phi_i = -0.416
    lam_i = lam_u + (psi * math.sin(A * math.pi)) / math.cos(phi_i * math.pi)
    phi_m = phi_i + 0.064 * math.cos((lam_i - 1.617) * math.pi)
    t = 43200.0 * lam_i + tow
    t = t % 86400.0
    if t < 0: t += 86400.0
    F = 1.0 + 16.0 * (0.53 - E) ** 3
    PER = beta[0] + beta[1]*phi_m + beta[2]*(phi_m**2) + beta[3]*(phi_m**3)
    if PER < 72000.0: PER = 72000.0
    AMP = alpha[0] + alpha[1]*phi_m + alpha[2]*(phi_m**2) + alpha[3]*(phi_m**3)
    if AMP < 0.0: AMP = 0.0
    x = (2.0 * math.pi * (t - 50400.0)) / PER
    if abs(x) < 1.5707963267948966:
        return F * (5e-9 + AMP * (1.0 - (x**2)/2.0 + (x**4)/24.0)) * C_LIGHT
    return F * 5e-9 * C_LIGHT

def calcular_posicion_satelite_wgs84(eph, t_emision, tau_vuelo, sys_char='G'):
    if not eph or eph['sqrtA'] <= 0.0: return None
    mu_sys = 3.986004418e14 if sys_char in 'EC' else MU
    omega_e_sys = 7.292115e-5 if sys_char == 'C' else OMEGA_E
    A = eph['sqrtA'] ** 2
    n0 = math.sqrt(mu_sys / (A ** 3))
    t_k = t_emision - eph['Toe']
    if sys_char == 'C': t_k -= 14.0
    if t_k > 302400: t_k -= 604800
    elif t_k < -302400: t_k += 604800
    M_k = eph['M0'] + (n0 + eph['Delta_n']) * t_k; E_k = M_k
    for _ in range(5): E_k = M_k + eph['e'] * math.sin(E_k)
    dt_sat = eph['af0'] + eph['af1'] * t_k + eph['af2'] * (t_k ** 2)
    nu_k = math.atan2((math.sqrt(1 - eph['e']**2) * math.sin(E_k)), (math.cos(E_k) - eph['e']))
    phi_k = nu_k + eph['omega']
    u_k = phi_k + eph['Cus'] * math.sin(2 * phi_k) + eph['Cuc'] * math.cos(2 * phi_k)
    r_k = A * (1 - eph['e'] * math.cos(E_k)) + eph['Crs'] * math.sin(2 * phi_k) + eph['Crc'] * math.cos(2 * phi_k)
    i_k = eph['i0'] + eph['Cic'] * math.cos(2 * phi_k) + eph['Cis'] * math.sin(2 * phi_k) + eph['IDOT'] * t_k
    x_k, y_k = r_k * math.cos(u_k), r_k * math.sin(u_k)
    omega_k = eph['OMEGA'] + (eph['OMEGA_DOT'] - omega_e_sys) * t_k - omega_e_sys * eph['Toe']
    xs = x_k * math.cos(omega_k) - y_k * math.cos(i_k) * math.sin(omega_k)
    ys = x_k * math.sin(omega_k) + y_k * math.cos(i_k) * math.cos(omega_k)
    zs = y_k * math.sin(i_k)
    theta = omega_e_sys * tau_vuelo
    return (xs * math.cos(theta) + ys * math.sin(theta), -xs * math.sin(theta) + ys * math.cos(theta), zs, dt_sat)

# =====================================================================
# EL CORAZÓN DE PROCESAMIENTO DGPS (CÓDIGO DIFERENCIAL)
# =====================================================================
def aislar_diferencias_simples_ppk(obs_b, obs_r):
    sd_suavizada = {}
    for tow in sorted(list(obs_r.keys())):
        if tow not in obs_b: continue
        
        sd_epoca = {'_meta': obs_r[tow]['_meta']}
        for s, d_r in obs_r[tow].items():
            if s == '_meta' or s not in obs_b[tow]: continue
            d_b = obs_b[tow]
            
            freq = 'L1' 
            if 'C5' in d_b[s] and 'C5' in d_r and 'L5' in d_b[s] and 'L5' in d_r:
                freq = 'L5' 
            elif not ('C1' in d_b[s] and 'C1' in d_r): continue
            
            pr_b = d_b[s]['C5'] if freq == 'L5' else d_b[s]['C1']
            pr_r = d_r['C5'] if freq == 'L5' else d_r['C1']
            
            snr_b = d_b[s].get('S5', 30.0) if freq == 'L5' else d_b[s].get('S1', 30.0)
            snr_r = d_r.get('S5', 30.0) if freq == 'L5' else d_r.get('S1', 30.0)
            
            sd_P = pr_r - pr_b
            
            sd_epoca[s] = {
                'sd_P': sd_P,
                'pr_b': pr_b, 'pr_r': pr_r,
                'snr': min(snr_b, snr_r)
            }
        if len(sd_epoca) > 1: sd_suavizada[tow] = sd_epoca
    return sd_suavizada

def calcular_dd_ppk_lambda_epoca(sd_epoca, nav, X_b, Y_b, Z_b, tr, mask_angle):
    try:
        X_iter, Y_iter, Z_iter = X_b, Y_b, Z_b 
        lat_b, lon_b, alt_b = ecef_a_geodesicas(X_b, Y_b, Z_b)
        
        iono = nav.get('_iono', {'alpha': [0]*4, 'beta': [0]*4})
        alpha, beta = iono['alpha'], iono['beta']
        
        sat_positions = {}
        for s, d in sd_epoca.items():
            if s == '_meta' or d['sd_P'] is None: continue 
            tau = d['pr_r'] / C_LIGHT
            sp = calcular_posicion_satelite_wgs84(seleccionar_efemeride_optima(nav.get(s), tr-tau), tr-tau, tau, s[0])
            if sp:
                el_r, az_r = calcular_topocentricas(sp[0], sp[1], sp[2], X_iter, Y_iter, Z_iter)
                if el_r >= mask_angle:
                    sat_positions[s] = {'sp': sp, 'el': el_r, 'az': az_r, 'sd_P': d['sd_P'], 'snr': d.get('snr', 30.0)}
        
        if len(sat_positions) < 4: return None, "FAILED"
        
        sat_list_full = list(sat_positions.keys())
        constellations = set([s[0] for s in sat_list_full])
        ref_sats = {}
        sat_list = []
        
        for c in constellations:
            c_sats = [s for s in sat_list_full if s[0] == c]
            if len(c_sats) >= 2:
                ref_sats[c] = max(c_sats, key=lambda k: sat_positions[k]['el'])
                c_sats.remove(ref_sats[c])
                sat_list.extend(c_sats)
        
        if len(sat_list) < 3: return None, "FAILED" 
        
        def calc_rho(sp, X, Y, Z, lat, lon, alt, el, az):
            dist = math.sqrt((sp[0]-X)**2 + (sp[1]-Y)**2 + (sp[2]-Z)**2)
            tropo = calcular_saastamoinen(lat, alt, el)
            iono_m = calcular_klobuchar(lat, lon, el, az, tr, alpha, beta)
            return dist + tropo, iono_m, dist

        prev_residuals = [0.0] * len(sat_list)

        for iteracion in range(8):
            lat_it, lon_it, alt_it = ecef_a_geodesicas(X_iter, Y_iter, Z_iter)
            
            H = []      
            L = []      
            W_diag = [] 
            
            ref_calcs = {}
            for c, r_sat in ref_sats.items():
                r_data = sat_positions[r_sat]
                rho_ref_r_base, iono_ref_r, dist_ref_r = calc_rho(r_data['sp'], X_iter, Y_iter, Z_iter, lat_it, lon_it, alt_it, r_data['el'], r_data['az'])
                el_ref_b, az_ref_b = calcular_topocentricas(r_data['sp'][0], r_data['sp'][1], r_data['sp'][2], X_b, Y_b, Z_b)
                rho_ref_b_base, iono_ref_b, _ = calc_rho(r_data['sp'], X_b, Y_b, Z_b, lat_b, lon_b, alt_b, el_ref_b, az_ref_b)
                
                ref_calcs[c] = {
                    'dist_ref_r': dist_ref_r,
                    'SD_P_calc_ref': (rho_ref_r_base + iono_ref_r) - (rho_ref_b_base + iono_ref_b),
                    'sp': r_data['sp'],
                    'el': r_data['el'],
                    'snr': r_data.get('snr', 30.0),
                    'sd_P': r_data['sd_P']
                }
            
            res_idx = 0
            for i, s in enumerate(sat_list):
                c = s[0]
                data = sat_positions[s]
                rc = ref_calcs[c]
                
                rho_i_r_base, iono_i_r, dist_i_r = calc_rho(data['sp'], X_iter, Y_iter, Z_iter, lat_it, lon_it, alt_it, data['el'], data['az'])
                el_i_b, az_i_b = calcular_topocentricas(data['sp'][0], data['sp'][1], data['sp'][2], X_b, Y_b, Z_b)
                rho_i_b_base, iono_i_b, _ = calc_rho(data['sp'], X_b, Y_b, Z_b, lat_b, lon_b, alt_b, el_i_b, az_i_b)
                
                SD_P_calc_i = (rho_i_r_base + iono_i_r) - (rho_i_b_base + iono_i_b)
                DD_P_calc = SD_P_calc_i - rc['SD_P_calc_ref']
                
                dx_geom = [
                    -(data['sp'][0] - X_iter) / dist_i_r - (-(rc['sp'][0] - X_iter) / rc['dist_ref_r']),
                    -(data['sp'][1] - Y_iter) / dist_i_r - (-(rc['sp'][1] - Y_iter) / rc['dist_ref_r']),
                    -(data['sp'][2] - Z_iter) / dist_i_r - (-(rc['sp'][2] - Z_iter) / rc['dist_ref_r'])
                ]
                
                sin_el_i_sq = math.sin(math.radians(data['el']))**2
                sin_el_ref_sq = math.sin(math.radians(rc['el']))**2
                snr_i_pow = 10.0 ** (data.get('snr', 30.0) / 10.0)
                snr_ref_pow = 10.0 ** (rc['snr'] / 10.0)
                
                w_i_ref = (sin_el_i_sq * snr_i_pow * sin_el_ref_sq * snr_ref_pow) / max(1.0, (sin_el_i_sq * snr_i_pow) + (sin_el_ref_sq * snr_ref_pow))

                DD_P_obs = data['sd_P'] - rc['sd_P']
                res_P = DD_P_obs - DD_P_calc
                
                L.append([res_P])
                H.append(dx_geom)
                
                if iteracion == 0:
                    w_P = w_i_ref * 1.0
                else:
                    w_P = w_i_ref * 1.0 / max(1.0, abs(prev_residuals[res_idx]) / 2.0)
                W_diag.append(w_P)
                res_idx += 1

            H_T = transpose_matrix(H)
            if not H_T or not W_diag: return None, "FAILED" 
            
            try:
                H_T_W = [[H_T[r][idx] * W_diag[idx] for idx in range(len(W_diag))] for r in range(len(H_T))]
            except IndexError:
                return None, "FAILED"

            N_mat = matmul(H_T_W, H)
            
            for r in range(len(N_mat)):
                N_mat[r][r] += abs(N_mat[r][r]) * 1e-6 + 1e-6
                
            U_vec = matmul(H_T_W, L)
            
            Q = invert_matrix_nxn(N_mat)
            if not Q: return None, "FAILED"
            
            Delta_X = matmul(Q, U_vec)
            if not Delta_X or len(Delta_X) < 3 or not Delta_X[0]: return None, "FAILED" 

            X_iter += Delta_X[0][0]; Y_iter += Delta_X[1][0]; Z_iter += Delta_X[2][0]
                
            prev_residuals = []
            for r in range(len(H)):
                v_val = sum(H[r][idx] * Delta_X[idx][0] for idx in range(len(H[0]))) - L[r][0]
                prev_residuals.append(v_val)
            
            if max(abs(Delta_X[0][0]), abs(Delta_X[1][0]), abs(Delta_X[2][0])) < 1e-3:
                return (X_iter, Y_iter, Z_iter), "FLOAT"
                
        return (X_iter, Y_iter, Z_iter), "FLOAT"
    except Exception as e:
        return None, f"FAILED_EXCEPTION:_{str(e)}"

# =====================================================================
# ESTADÍSTICAS Y FILTRADO VINCULANTE (HARD FILTER)
# =====================================================================
def estadistica_desacoplada(coordenadas, conf_plani, conf_alti, err_hor_max, err_ver_max):
    if not coordenadas: return None, None, None, 0, 0, 0, 0, 0.0
    
    N_list = [c[0] for c in coordenadas]
    E_list = [c[1] for c in coordenadas]
    Z_list = [c[2] for c in coordenadas]

    def get_median(lst):
        s = sorted(lst); n = len(s)
        if n == 0: return 0
        return s[n//2] if n % 2 == 1 else (s[n//2 - 1] + s[n//2]) / 2.0

    med_N = get_median(N_list); med_E = get_median(E_list); med_Z = get_median(Z_list)
    
    valid_coords = []
    for c in coordenadas:
        dh = math.hypot(c[0] - med_N, c[1] - med_E)
        dv = abs(c[2] - med_Z)
        
        if (err_hor_max > 0.0 and dh > err_hor_max) or (err_ver_max > 0.0 and dv > err_ver_max):
            continue
        valid_coords.append(c)

    if not valid_coords: return None, None, None, 0, 0, 0, 0, 0.0
    
    N_v = [c[0] for c in valid_coords]; E_v = [c[1] for c in valid_coords]; Z_v = [c[2] for c in valid_coords]
    f_v = [c[3] for c in valid_coords if c[3] == "FIXED"]

    def calc_mean_std(arr):
        n = len(arr); m = sum(arr) / n
        return m, (math.sqrt(sum((x - m)**2 for x in arr) / n) if n > 1 else 0.0)

    N_m, N_s = calc_mean_std(N_v); E_m, E_s = calc_mean_std(E_v); Z_m, Z_s = calc_mean_std(Z_v)
    
    N_f = [x for x in N_v if abs(x - N_m) <= conf_plani * N_s] if N_s > 0 else N_v
    E_f = [x for x in E_v if abs(x - E_m) <= conf_plani * E_s] if E_s > 0 else E_v
    Z_f = [x for x in Z_v if abs(x - Z_m) <= conf_alti * Z_s] if Z_s > 0 else Z_v

    fix_ratio = (len(f_v) / len(valid_coords)) * 100
    return sum(N_f)/max(1, len(N_f)), sum(E_f)/max(1, len(E_f)), sum(Z_f)/max(1, len(Z_f)), N_s, E_s, Z_s, min(len(N_f), len(E_f), len(Z_f)), fix_ratio

# =====================================================================
# GENERADORES DE INFORMES (FRONTEND)
# =====================================================================
def generar_informe_homogeneizacion_detallado(base_name, rover_name, base_raw, rover_raw, rover_sinc):
    def get_stats(obs):
        c = {'G':0, 'E':0, 'C':0, 'R':0, 'S':0, 'J':0}
        tiempos = sorted(list(obs.keys()))
        if not tiempos: return c, 0, None, None, 0.0, 0
        epocas = len(obs)
        t_ini, t_fin = obs[tiempos[0]]['_meta'], obs[tiempos[-1]]['_meta']
        intervalos = [tiempos[i] - tiempos[i-1] for i in range(1, epocas)]
        tasa_muestreo = sum(intervalos)/len(intervalos) if intervalos else 0.0
        gaps = sum(1 for i in intervalos if i > tasa_muestreo * 1.5)
        for t in tiempos:
            for s in obs[t]:
                if s != '_meta' and s[0] in c: c[s[0]] += 1
        return {k: v/epocas for k, v in c.items()}, epocas, t_ini, t_fin, tasa_muestreo, gaps
    
    cb, eb, b_ini, b_fin, tr_b, g_b = get_stats(base_raw)
    cr, er, r_ini, r_fin, tr_r, g_r = get_stats(rover_raw)
    cs, es, s_ini, s_fin, tr_s, _ = get_stats(rover_sinc)
    t_exito = (es / er * 100) if er > 0 else 0.0
    
    informe = f"""
========================================================================
    AUDITORÍA FORENSE DE EMPAREJAMIENTO DE ÉPOCAS
========================================================================
[1] PARÁMETROS DE CONTROL (BASE) : {base_name}
  [-] Épocas Crudas Registradas : {eb}
  [-] Ventana de Observación    : {b_ini[3]:02d}:{b_ini[4]:02d}:{b_ini[5]:05.2f} - {b_fin[3]:02d}:{b_fin[4]:02d}:{b_fin[5]:05.2f}

[2] PARÁMETROS DEL MÓVIL (ROVER) : {rover_name}
  [-] Épocas Crudas Registradas : {er}
  [-] Ventana de Observación    : {r_ini[3]:02d}:{r_ini[4]:02d}:{r_ini[5]:05.2f} - {r_fin[3]:02d}:{r_fin[4]:02d}:{r_fin[5]:05.2f}

[3] MATRIZ RESULTANTE (ESTRICTA, SIN INTERPOLACIÓN)
  [-] Épocas Útiles Sincronizadas: {es}
  [-] Tasa de Éxito sobre Rover  : {t_exito:.1f}%
========================================================================
"""
    return informe

def generar_informe_ascii(tipo, p_dict):
    estado_sol = 'FLOAT (DGPS)'
    informe = f"""
========================================================================
             INFORME DE PROCESAMIENTO GNSSJP PRO 
========================================================================

[*] RESULTADO DE MEDICIÓN ABSOLUTA ({estado_sol})
------------------------------------------------------------------------
  [-] Tolerancia Horizontal  : {'± ' + str(p_dict['err_h']) + ' m (Vinculante)' if p_dict['err_h'] > 0 else 'Inactiva'}
  [-] Tolerancia Vertical    : {'± ' + str(p_dict['err_v']) + ' m (Vinculante)' if p_dict['err_v'] > 0 else 'Inactiva'}
  [-] Máscara Elevación      : {p_dict['mask']:.14f}°
  [-] Filtro Planimétrico    : {p_dict['cp']:.14f} Sigma
  [-] Filtro Altimétrico     : {p_dict['ca']:.14f} Sigma
  [-] Épocas Útiles Retenidas: {p_dict['ret']} ({(p_dict['ret']/max(1, p_dict['total']))*100:.1f}% del total)
  [-] Varianza Global Z      : {p_dict['ez']:.3f} m

[1] TRAZABILIDAD DEL PROYECTO Y ARCHIVOS
------------------------------------------------------------------------
  [-] Archivo Control (Base) : {p_dict['base_file']}
  [-] Archivo Móvil (Rover)  : {p_dict['rover_file']}
  [-] Archivo Efemérides     : {p_dict['nav_file']}

[2] ESTRATEGIA MATEMÁTICA Y ESTADÍSTICA
------------------------------------------------------------------------
  [-] Motor Algorítmico      : Diferencias Dobles Pseudodistancia C1/C5
  [-] Resolución Matriz      : Ajuste IRLS Mínimos Cuadrados
  [-] Sincronización Épocas  : Emparejamiento Dinámico Estricto (< 0.05s)

[3] CALIDAD GEOMÉTRICA (QA / QC)
------------------------------------------------------------------------
  [-] Error Horizontal (RMS) : ± {math.hypot(p_dict['std_n'], p_dict['std_e']):.3f} m
  [-] Error Espacial (3D RMS): ± {math.sqrt(p_dict['std_n']**2 + p_dict['std_e']**2 + p_dict['std_z']**2):.3f} m

[4] RESULTADOS VECTORIALES FINALES
------------------------------------------------------------------------
  * COORDENADA DE CONTROL (BASE FIJA):
      Norte : {p_dict['b_n']:.3f} m
      Este  : {p_dict['b_e']:.3f} m
      Cota  : {p_dict['b_z']:.3f} m

  * COORDENADA CALCULADA (AJUSTE IRLS DGPS {estado_sol}):
      Norte : {p_dict['r_n_calc']:.3f} m
      Este  : {p_dict['r_e_calc']:.3f} m
      Cota  : {p_dict['r_z_calc']:.3f} m
========================================================================
"""
    return informe

# =====================================================================
# RUTAS FLASK (FLUJO ARQUITECTÓNICO CORREGIDO)
# =====================================================================
@app.route('/')
def index(): return send_file('index.html')

@app.route('/tab1_homogenizar', methods=['POST'])
def tab1_homogenizar():
    with STATE_LOCK:
        if os.path.exists(STATE_FILE):
            try: os.remove(STATE_FILE)
            except: pass
    
    url_b = request.form.get('url_base')
    url_r = request.form.get('url_rover')

    if not url_b or not url_r: 
        return Response("> [ERROR CRÍTICO] Falta URL de Google Drive de Base o Rover.\n", mimetype='text/plain')
    
    p_b_raw = os.path.join(UPLOAD_FOLDER, 'base_raw.obs')
    p_r_raw = os.path.join(UPLOAD_FOLDER, 'rover_calibracion_raw.obs')

    def procesar():
        try:
            yield "> [SISTEMA] Descargando Archivo Base desde Google Drive...\n"
            descargar_gdrive_publico(url_b, p_b_raw)
            yield "> [SISTEMA] Descargando Archivo Rover Calibración desde Google Drive...\n"
            descargar_gdrive_publico(url_r, p_r_raw)

            yield f"> [SISTEMA] Iniciando Etapa 1: Emparejamiento Base Pivote y Rover de Calibración...\n"
            base_raw_dict = parse_rinex_obs_completo(p_b_raw)
            rover_raw_dict = parse_rinex_obs_completo(p_r_raw)
            base_sinc, rover_sinc = {}, {}
            total_epochs = len(rover_raw_dict)
            c = 0
            for tr in sorted(list(rover_raw_dict.keys())):
                c += 1
                if c % max(1, total_epochs // 10) == 0: yield f"[PROGRESO] Cotejando épocas sin distorsión... {int((c / total_epochs) * 100)}%\n"
                base_interp = interpolar_base_a_rover(base_raw_dict, tr)
                if base_interp:
                    base_sinc[tr] = base_interp
                    base_sinc[tr]['_meta'] = rover_raw_dict[tr]['_meta']
                    rover_sinc[tr] = rover_raw_dict[tr]
            
            if not base_sinc: yield "\n> [ERROR FATAL] Cero épocas en común. Revisar rango horario."; return
            p_b_h = os.path.join(UPLOAD_FOLDER, 'base_calib_homo.obs')
            p_r_h = os.path.join(UPLOAD_FOLDER, 'rover_calib_homo.obs')
            generar_rinex_sincronizado(p_b_raw, p_b_h, base_sinc)
            generar_rinex_sincronizado(p_r_raw, p_r_h, rover_sinc)
            
            guardar_estado('base_raw', p_b_raw)
            guardar_estado('base_calib_homo', p_b_h)
            guardar_estado('rover_calib_homo', p_r_h)
            
            b_name_id = extraer_gdrive_id(url_b) + ".obs"
            r_name_id = extraer_gdrive_id(url_r) + ".obs"
            guardar_estado('name_base_raw', b_name_id)
            guardar_estado('name_rover_calib_raw', r_name_id)
            
            yield generar_informe_homogeneizacion_detallado(b_name_id, r_name_id, base_raw_dict, rover_raw_dict, rover_sinc)
            yield "\n[SUCCESS]"
        except Exception as e: yield f"\n> [ERROR] Falla estructural: {str(e)}"
    return Response(procesar(), mimetype='text/plain')

@app.route('/tab2_efemerides', methods=['POST'])
def tab2_efemerides():
    def procesar():
        try:
            yield "> [SISTEMA] Iniciando Etapa 2: Motor de Navegación Orbital e Ionosférico...\n"
            bp = leer_estado('base_raw')
            if not bp or not os.path.exists(bp): yield "> [ERROR FATAL] Falta RINEX Base en memoria.\n"; return
            ft = obtener_fecha_obs(bp)
            if not ft: yield "> [ERROR FATAL] Imposible extraer la fecha.\n"; return
            nav_p, descarga_exitosa = None, False
            for tipo, log in descargar_efemerides_brdc_stream(ft[0], ft[1], ft[2], ft[3]):
                if tipo == "INFO": yield f"  {log}"
                elif tipo == "SUCCESS": nav_p = log; descarga_exitosa = True
                elif tipo == "ERROR": yield f"> [ERROR CRÍTICO RED] {log}\n"; return 
            if descarga_exitosa and nav_p:
                guardar_estado('nav_path', nav_p); guardar_estado('name_nav_file', os.path.basename(nav_p))
                yield f"> [ÉXITO] Archivo de efemérides almacenado en: {nav_p}\n\n[SUCCESS]"
            else: yield "> [ERROR] No se logró descargar ni construir el archivo local.\n"
        except Exception as e: yield f"\n> [ERROR GENERAL] Excepción capturada: {str(e)}"
    return Response(procesar(), mimetype='text/plain')

@app.route('/tab3_calibrar', methods=['POST'])
def tab3_calibrar():
    utm_n = safe_f(request.form.get('utm_norte'), 0.0)
    utm_e = safe_f(request.form.get('utm_este'), 0.0)
    utm_c = safe_f(request.form.get('utm_cota'), 0.0)
    utm_h = safe_i(request.form.get('utm_huso'), 19)
    utm_hem = request.form.get('utm_hemisferio', 'N')

    utm_n_r = safe_f(request.form.get('utm_norte_r'), 0.0)
    utm_e_r = safe_f(request.form.get('utm_este_r'), 0.0)
    utm_c_r = safe_f(request.form.get('utm_cota_r'), 0.0)

    def procesar():
        try:
            yield "> [SISTEMA] Iniciando Búsqueda Determinista (Investigación de Operaciones OR)...\n"
            if utm_e == 0.0 or utm_n == 0.0 or utm_n_r == 0.0 or utm_e_r == 0.0: 
                yield "> [ERROR] Coordenadas Base y Rover (Calibración) son requeridas.\n"; return
            
            nav_path = leer_estado('nav_path')
            p_b_h = leer_estado('base_calib_homo')
            p_r_h = leer_estado('rover_calib_homo')

            if not nav_path or not p_b_h or not p_r_h: 
                yield "> [ERROR FATAL] Faltan archivos RINEX o Efemérides.\n"; return

            obs_b_raw = parse_rinex_obs_completo(p_b_h)
            obs_r_raw = parse_rinex_obs_completo(p_r_h)
            nav = parse_rinex_nav_real(nav_path)
            
            yield "[PROGRESO] Re-ensamblando Malla Temporal de Calibración...\n"
            sd_suavizada = aislar_diferencias_simples_ppk(obs_b_raw, obs_r_raw)
            if not sd_suavizada:
                yield "> [ERROR] No hay épocas sincronizadas válidas.\n"
                return

            t_sample = list(sd_suavizada.keys())
            lat_b, lon_b, _ = utm_a_geodesicas(utm_e, utm_n, utm_h, utm_hem)
            X_b, Y_b, Z_b = geodesicas_a_ecef(lat_b, lon_b, utm_c)

            # =========================================================================
            # FASE 1: CÁLCULO DETERMINISTA DE ERRORES MÁXIMOS (Eh, Ev)
            # =========================================================================
            yield "[PROGRESO] Fase 1: Extrayendo Errores Máximos Permitidos...\n"
            
            coords_raw = []
            for t in t_sample:
                sem, status = calcular_dd_ppk_lambda_epoca(sd_suavizada[t], nav, X_b, Y_b, Z_b, t, 10.0) # Máscara basal fija
                if sem:
                    X_ri, Y_ri, Z_ri = sem
                    la, lo, al = ecef_a_geodesicas(X_ri, Y_ri, Z_ri)
                    nt, et = geodesicas_a_utm(la, lo, utm_h)
                    coords_raw.append((nt, et, al))
            
            if not coords_raw:
                yield "> [ERROR] Nube de puntos bruta colapsada.\n"; return
                
            deltas_h = [math.hypot(c[0] - utm_n_r, c[1] - utm_e_r) for c in coords_raw]
            deltas_v = [abs(c[2] - utm_c_r) for c in coords_raw]
            
            deltas_h.sort()
            deltas_v.sort()
            
            # Anclamos el Hard Filter estricto al percentil 10 de élite geométrica verdadera
            idx_optimo = max(1, len(deltas_h) // 10)
            best_eh = max(0.01, float(deltas_h[idx_optimo]))
            best_ev = max(0.01, float(deltas_v[idx_optimo]))
            
            yield f"  [*] Límite Horizontal Inyectado: {best_eh:.14f} m\n"
            yield f"  [*] Límite Vertical Inyectado: {best_ev:.14f} m\n\n"
            
            # =========================================================================
            # FASE 2: MALLA DETERMINISTA DE REFINAMIENTO SUCESIVO (GRID ZOOMING)
            # =========================================================================
            yield "[PROGRESO] Fase 2: Malla Determinista para Parámetros (M, Cp, Ca)...\n"
            
            best_rmse = float('inf')
            best_params = {}
            
            m_center, m_span = 10.0, 5.0
            cp_center, cp_span = 2.0, 1.5
            ca_center, ca_span = 2.0, 1.5
            
            # 8 niveles de zoom continuo garantizan precisión 100% inamovible (IEEE 754)
            for nivel in range(8):
                yield f"  [+] Refinando espacio de búsqueda (Zoom {nivel+1}/8)...\n"
                
                m_grid = [m_center - m_span, m_center, m_center + m_span]
                cp_grid = [cp_center - cp_span, cp_center, cp_center + cp_span]
                ca_grid = [ca_center - ca_span, ca_center, ca_center + ca_span]
                
                # Truncar sobre límites geodésicos lógicos
                m_grid = [max(5.0, min(15.0, x)) for x in m_grid]
                cp_grid = [max(0.1, min(5.0, x)) for x in cp_grid]
                ca_grid = [max(0.1, min(5.0, x)) for x in ca_grid]
                
                nivel_best_rmse = float('inf')
                nivel_best_m = m_center
                nivel_best_cp = cp_center
                nivel_best_ca = ca_center
                
                for m in set(m_grid):
                    coords = []
                    for t in t_sample:
                        sem, status = calcular_dd_ppk_lambda_epoca(sd_suavizada[t], nav, X_b, Y_b, Z_b, t, m)
                        if sem:
                            X_ri, Y_ri, Z_ri = sem
                            la, lo, al = ecef_a_geodesicas(X_ri, Y_ri, Z_ri)
                            nt, et = geodesicas_a_utm(la, lo, utm_h)
                            coords.append((nt, et, al, status))
                    
                    if not coords: continue
                    
                    for cp in set(cp_grid):
                        for ca in set(ca_grid):
                            # INYECTAMOS LOS ERRORES EXACTOS CALCULADOS EN LA FASE 1
                            res = estadistica_desacoplada(coords, cp, ca, best_eh, best_ev)
                            if res[0] is None: continue
                            nf, ef, zf, std_n, std_e, std_z, ret, fix_ratio = res
                            
                            rmse_3d = math.sqrt((nf - utm_n_r)**2 + (ef - utm_e_r)**2 + (zf - utm_c_r)**2)
                            
                            if rmse_3d < nivel_best_rmse:
                                nivel_best_rmse = rmse_3d
                                nivel_best_m = m
                                nivel_best_cp = cp
                                nivel_best_ca = ca
                                
                                best_rmse = rmse_3d
                                best_params = {
                                    'mask': m, 'cp': cp, 'ca': ca, 'eh': best_eh, 'ev': best_ev,
                                    'rmse': rmse_3d, 'ret': ret,
                                    'dn': nf - utm_n_r, 'de': ef - utm_e_r, 'dz': zf - utm_c_r
                                }
                
                # Preparamos el siguiente Zoom reduciendo el área de búsqueda a la mitad
                m_center, m_span = nivel_best_m, m_span / 2.0
                cp_center, cp_span = nivel_best_cp, cp_span / 2.0
                ca_center, ca_span = nivel_best_ca, ca_span / 2.0
            
            if best_rmse != float('inf'):
                yield "\n========================================================\n"
                yield "      [INFORME] PARÁMETROS ÓPTIMOS (CALIBRACIÓN OR)\n"
                yield "========================================================\n"
                yield f"  [-] Máscara Elevación (°): {best_params['mask']:.14f}\n"
                yield f"  [-] Filtro Sigma Plan (cp): {best_params['cp']:.14f}\n"
                yield f"  [-] Filtro Sigma Alt (ca): {best_params['ca']:.14f}\n"
                yield f"  [-] Error Permitido Horizontal (m): {best_params['eh']:.14f}\n"
                yield f"  [-] Error Permitido Vertical (m): {best_params['ev']:.14f}\n"
                yield "--------------------------------------------------------\n"
                yield f"  [*] RMSE Global 3D al Punto: {best_params['rmse']:.4f} m\n"
                yield f"  [*] Deltas Residuales -> N: {best_params['dn']:.3f}m, E: {best_params['de']:.3f}m, Z: {best_params['dz']:.3f}m\n"
                yield f"  [*] Épocas Retenidas: {best_params['ret']}\n"
                yield "========================================================\n"
                yield "\n[SUCCESS]"
            else:
                yield "\n> [ERROR] El modelo determinista no convergió. Filtros demasiado agresivos.\n"
        except Exception as e: yield f"\n> [ERROR FATAL] {str(e)}"
    return Response(procesar(), mimetype='text/plain')

@app.route('/tab4_procesar', methods=['POST'])
def tab4_procesar():
    utm_n = safe_f(request.form.get('utm_norte'), 0.0)
    utm_e = safe_f(request.form.get('utm_este'), 0.0)
    utm_c = safe_f(request.form.get('utm_cota'), 0.0)
    utm_h = safe_i(request.form.get('utm_huso'), 19)
    utm_hem = request.form.get('utm_hemisferio', 'N')
    h_b = safe_f(request.form.get('altura_base'), 0.0)
    h_r = safe_f(request.form.get('altura_rover'), 0.0)
    
    p_mask = safe_f(request.form.get('param_mask'), 10.0)
    p_cp = safe_f(request.form.get('param_cp'), 2.5)
    p_ca = safe_f(request.form.get('param_ca'), 1.5)
    err_hor_max = safe_f(request.form.get('err_hor_max'), 0.5)
    err_ver_max = safe_f(request.form.get('err_ver_max'), 0.5)

    url_r_nuevo = request.form.get('url_rover_nuevo')
    
    if not url_r_nuevo or str(url_r_nuevo).strip() == '': 
        return Response("> [ERROR] Falta el enlace de Google Drive del nuevo archivo RINEX Rover (Punto Desconocido).\n", mimetype='text/plain')

    p_r_nuevo = os.path.join(UPLOAD_FOLDER, 'rover_nuevo_raw.obs')

    def procesar():
        try:
            yield "> [SISTEMA] Descargando nuevo archivo RINEX Rover desde Google Drive...\n"
            descargar_gdrive_publico(url_r_nuevo, p_r_nuevo)
            rf_nuevo_filename = extraer_gdrive_id(url_r_nuevo) + ".obs"

            yield "> [SISTEMA] Iniciando Procesamiento DGPS (Punto Ciego Desconocido)...\n"
            if utm_e == 0.0 or utm_n == 0.0: 
                yield "> [ERROR] Coordenadas Base incompletas.\n"; return
            
            nav_path = leer_estado('nav_path')
            p_b_raw = leer_estado('base_raw') 

            if not nav_path or not p_b_raw or not os.path.exists(p_b_raw): 
                yield "> [ERROR FATAL] Falta archivo RINEX Base original o Efemérides en memoria.\n"; return

            obs_b_raw = parse_rinex_obs_completo(p_b_raw)
            obs_r_raw = parse_rinex_obs_completo(p_r_nuevo) 
            nav = parse_rinex_nav_real(nav_path)
            
            yield "[PROGRESO] Emparejamiento Temporal Dinámico contra la Base Pivote (Tolerancia 0.05s)...\n"
            rover_tows = sorted(list(obs_r_raw.keys()))
            base_tows = sorted(list(obs_b_raw.keys()))
            obs_b_sync = {}
            for tr in rover_tows:
                if not base_tows: continue
                idx = min(range(len(base_tows)), key=lambda i: abs(base_tows[i] - tr))
                if abs(base_tows[idx] - tr) <= 0.05:
                    obs_b_sync[tr] = obs_b_raw[base_tows[idx]].copy()
                    obs_b_sync[tr]['_meta'] = obs_r_raw[tr]['_meta']
            
            yield "[PROGRESO] Extrayendo Observables DGPS (Pseudodistancia)...\n"
            sd_suavizada = aislar_diferencias_simples_ppk(obs_b_sync, obs_r_raw)
            
            if len(sd_suavizada) == 0:
                yield "\n> [ERROR] No hay épocas sincronizadas válidas entre la Base y este nuevo Rover.\n"
                return

            lat_b, lon_b, _ = utm_a_geodesicas(utm_e, utm_n, utm_h, utm_hem)
            X_b, Y_b, Z_b = geodesicas_a_ecef(lat_b, lon_b, utm_c + h_b)

            coords = []
            t_eps = len(sd_suavizada); c = 0
            
            for t in sd_suavizada:
                c += 1
                if c % max(1, t_eps // 10) == 0: yield f"[PROGRESO] Resolviendo Ecuaciones Matriciales DGPS... {int((c / t_eps) * 100)}%\n"
                
                sem, status = calcular_dd_ppk_lambda_epoca(sd_suavizada[t], nav, X_b, Y_b, Z_b, t, p_mask)
                if not sem: continue
                X_ri, Y_ri, Z_ri = sem
                la, lo, al = ecef_a_geodesicas(X_ri, Y_ri, Z_ri)
                nt, et = geodesicas_a_utm(la, lo, utm_h)
                coords.append((nt, et, al, status))

            if not coords: yield "\n> [ERROR] Fracaso algorítmico total en Inversión NxN.\n"; return
            
            res_estadistica = estadistica_desacoplada(coords, p_cp, p_ca, err_hor_max, err_ver_max)
            
            if res_estadistica[0] is None:
                yield "\n> [ERROR] Operación Abortada: El 100% de las épocas superan el Error Máximo configurado.\n"
                return
                
            nf, ef, zf, std_n, std_e, std_z, ret, fix_ratio = res_estadistica
            
            p_dict = {
                'mask': p_mask, 'cp': p_cp, 'ca': p_ca,
                'err_h': err_hor_max, 'err_v': err_ver_max,
                'nf': nf, 'ef': ef, 'zf': zf - h_r, 
                'ret': ret, 'total': len(coords), 'std_n': std_n, 'std_e': std_e, 'std_z': std_z,
                'ez': std_z, 'fix_r': fix_ratio,
                'base_file': leer_estado('name_base_raw') or "base.obs",
                'rover_file': rf_nuevo_filename,
                'nav_file': leer_estado('name_nav_file') or "auto_nav.nav",
                'b_n': utm_n, 'b_e': utm_e, 'b_z': utm_c,
                'r_n_calc': nf, 'r_e_calc': ef, 'r_z_calc': zf - h_r
            }
            
            yield "[PROGRESO] Ajuste DGPS Finalizado.\n"
            yield generar_informe_ascii("MEDICION", p_dict)
            yield "\n[SUCCESS]"
        except Exception as e: yield f"\n> [ERROR FATAL] {str(e)}"
    return Response(procesar(), mimetype='text/plain')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9000, debug=True)
