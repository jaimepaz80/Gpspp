import os
import math
import datetime
import urllib.request
import gzip
import shutil
import ssl
import json
import re
from flask import Flask, request, send_file, Response

app = Flask(__name__)

BASE_DIR = '/tmp'
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'temp_rinex')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

C_LIGHT = 299792458.0
OMEGA_E = 7.2921151467e-5
MU = 3.986005e14

def extraer_gdrive_id(url):
    match = re.search(r'/file/d/([a-zA-Z0-9_-]+)', url)
    if match: return match.group(1)
    match = re.search(r'id=([a-zA-Z0-9_-]+)', url)
    if match: return match.group(1)
    return None

def descargar_gdrive_publico(url, dest_path):
    file_id = extraer_gdrive_id(url)
    if not file_id: raise Exception("Formato de URL de Google Drive inválido.")
    download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    req = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as res:
        with open(dest_path, 'wb') as f: f.write(res.read())

def safe_f(val, default=0.0):
    try: return float(val) if val and str(val).strip() != '' else default
    except: return default

def safe_i(val, default=19):
    try: return int(val) if val and str(val).strip() != '' else default
    except: return default

def gps_time_to_tow(year, month, day, hour, minute, second):
    sec_int, sec_frac = int(second), second - int(second)
    total = (datetime.datetime(year, month, day, hour, minute, sec_int) - datetime.datetime(1980, 1, 6)).total_seconds() + sec_frac
    return total - (int(total // 604800) * 604800)

def simular_escritura_disco_js(obs):
    truncado = {}
    for t, data_t in obs.items():
        truncado[t] = {'_meta': data_t.get('_meta')}
        for sat, data_sat in data_t.items():
            if sat == '_meta': continue
            truncado[t][sat] = {}
            if 'C1' in data_sat and data_sat['C1'] > 0: truncado[t][sat]['C1'] = round(data_sat['C1'], 3)
            if 'L1' in data_sat and data_sat['L1'] > 0: truncado[t][sat]['L1'] = round(data_sat['L1'], 3)
            if 'C5' in data_sat and data_sat['C5'] > 0: truncado[t][sat]['C5'] = round(data_sat['C5'], 3)
            if 'L5' in data_sat and data_sat['L5'] > 0: truncado[t][sat]['L5'] = round(data_sat['L5'], 3)
    return truncado

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
                idx_c1, idx_c5 = sys_idx.get(sys_char, {}).get('C1', -1), sys_idx.get(sys_char, {}).get('C5', -1)
                idx_l1, idx_l5 = sys_idx.get(sys_char, {}).get('L1', -1), sys_idx.get(sys_char, {}).get('L5', -1)
                idx_s1, idx_s5 = sys_idx.get(sys_char, {}).get('S1', -1), sys_idx.get(sys_char, {}).get('S5', -1)
                
                data = {}
                def get_val(idx):
                    if idx >= 0 and len(line) >= 17 + 16 * idx:
                        v = line[3+16*idx : 17+16*idx].strip()
                        if v: return float(v.replace('D', 'E').replace('d', 'e'))
                    return None
                
                v_c1, v_l1, v_c5, v_l5 = get_val(idx_c1), get_val(idx_l1), get_val(idx_c5), get_val(idx_l5)
                v_s1, v_s5 = get_val(idx_s1), get_val(idx_s5)
                
                if v_c1 is not None: data['C1'] = v_c1
                if v_l1 is not None: data['L1'] = v_l1
                if v_c5 is not None: data['C5'] = v_c5
                if v_l5 is not None: data['L5'] = v_l5
                if v_s1 is not None: data['S1'] = v_s1
                if v_s5 is not None: data['S5'] = v_s5
                
                if ('C1' in data and data['C1'] > 15000000.0) or ('C5' in data and data['C5'] > 15000000.0):
                    obs[tow][line[0:3].strip()] = data
    return obs

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
                        except: vals.append(0.0)
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
    ephemeris['_iono'] = {'alpha': iono_params['GPSA'] if any(iono_params['GPSA']) else iono_params['BDSA'], 'beta': iono_params['GPSB'] if any(iono_params['GPSB']) else iono_params['BDSB']}
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
                        y = int(partes[0])
                        return (y + 2000 if y < 100 else y), int(partes[1]), int(partes[2]), int(partes[3]), int(partes[4]), float(partes[5])
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
    for p in prefijos:
        for h in [hour] + list(range(hour-1, -1, -1)) + list(range(hour+1, 24)): 
            urls.append(f"https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{year}/{doy:03d}/BRDC00{p}_R_{year}{doy:03d}{h:02d}00_01H_MN.rnx.gz")
    ctx = ssl.create_default_context()
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, context=ctx, timeout=10) as res:
                yield ("INFO", f"> Descargando comprimido: {url.split('/')[-1]}...\n")
                with open(nav_descargado + '.gz', 'wb') as f: f.write(res.read())
                yield ("INFO", "> Descomprimiendo GZIP y construyendo .nav local...\n")
                with gzip.open(nav_descargado + '.gz', 'rb') as f_in, open(nav_descargado, 'wb') as f_out: shutil.copyfileobj(f_in, f_out)
                yield ("SUCCESS", nav_descargado)
                return
        except Exception: pass
    yield ("ERROR", "Falla catastrófica al conectar con IGS/BKG.")

def transpose_matrix(M):
    if not M or not M[0]: return []
    try: return [[M[j][i] for j in range(len(M))] for i in range(len(M[0]))]
    except: return []

def matmul(A, B):
    if not A or not B or not A[0] or not B[0]: return []
    try:
        res = [[0.0]*len(B[0]) for _ in range(len(A))]
        for i in range(len(A)):
            for j in range(len(B[0])):
                for k in range(len(B)): res[i][j] += A[i][k] * B[k][j]
        return res
    except: return []

def invert_matrix_nxn(M):
    if not M or not M[0]: return None
    try:
        n = len(M)
        A = [[float(M[i][j]) for j in range(n)] for i in range(n)]
        I = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
        for i in range(n):
            max_k = i
            for k in range(i + 1, n):
                if abs(A[k][i]) > abs(A[max_k][i]): max_k = k
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
    except: return None

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
    dx, dy, dz = xs - X_usr, ys - Y_usr, zs - Z_usr
    sin_lat, cos_lat = math.sin(math.radians(lat_val)), math.cos(math.radians(lat_val))
    sin_lon, cos_lon = math.sin(math.radians(lon_val)), math.cos(math.radians(lon_val))
    e = -sin_lon * dx + cos_lon * dy
    n = -sin_lat * cos_lon * dx - sin_lat * sin_lon * dy + cos_lat * dz
    u = cos_lat * cos_lon * dx + cos_lat * sin_lon * dy + sin_lat * dz
    dist = math.sqrt(dx**2 + dy**2 + dz**2)
    if dist < 1e-6: return 0.0, 0.0
    el = math.degrees(math.asin(max(-1.0, min(1.0, u / dist))))
    az = math.degrees(math.atan2(e, n))
    return el, az + 360.0 if az < 0 else az

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
    t = (43200.0 * lam_i + tow) % 86400.0
    if t < 0: t += 86400.0
    F = 1.0 + 16.0 * (0.53 - E) ** 3
    PER = beta[0] + beta[1]*phi_m + beta[2]*(phi_m**2) + beta[3]*(phi_m**3)
    if PER < 72000.0: PER = 72000.0
    AMP = alpha[0] + alpha[1]*phi_m + alpha[2]*(phi_m**2) + alpha[3]*(phi_m**3)
    if AMP < 0.0: AMP = 0.0
    x = (2.0 * math.pi * (t - 50400.0)) / PER
    if abs(x) < 1.5707963267948966: return F * (5e-9 + AMP * (1.0 - (x**2)/2.0 + (x**4)/24.0)) * C_LIGHT
    return F * 5e-9 * C_LIGHT

def calcular_posicion_satelite_wgs84(eph, t_emision, tau_vuelo, sys_char='G'):
    if not eph or eph['sqrtA'] <= 0.0: return None
    mu_sys = 3.986004418e14 if sys_char in 'EC' else MU
    omega_e_sys = 7.292115e-5 if sys_char == 'C' else OMEGA_E
    A = eph['sqrtA'] ** 2
    n0 = math.sqrt(mu_sys / (A ** 3))
    t_k = t_emision - eph['Toe'] - (14.0 if sys_char == 'C' else 0.0)
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

def aislar_diferencias_simples_ppk(obs_b, obs_r):
    sd_suavizada = {}
    for tow in sorted(list(obs_r.keys())):
        if tow not in obs_b: continue
        sd_epoca = {'_meta': obs_r[tow]['_meta']}
        for s, d_r in obs_r[tow].items():
            if s == '_meta' or s not in obs_b[tow]: continue
            d_b = obs_b[tow]
            freq = 'L5' if ('C5' in d_b[s] and 'C5' in d_r and 'L5' in d_b[s] and 'L5' in d_r) else 'L1'
            if freq == 'L1' and not ('C1' in d_b[s] and 'C1' in d_r): continue
            pr_b = d_b[s]['C5'] if freq == 'L5' else d_b[s]['C1']
            pr_r = d_r['C5'] if freq == 'L5' else d_r['C1']
            snr_b = d_b[s].get('S5', 30.0) if freq == 'L5' else d_b[s].get('S1', 30.0)
            snr_r = d_r.get('S5', 30.0) if freq == 'L5' else d_r.get('S1', 30.0)
            sd_epoca[s] = {'sd_P': pr_r - pr_b, 'pr_b': pr_b, 'pr_r': pr_r, 'snr': min(snr_b, snr_r)}
        if len(sd_epoca) > 1: sd_suavizada[tow] = sd_epoca
    return sd_suavizada

def calcular_dd_ppk_lambda_epoca(sd_epoca, nav, X_b, Y_b, Z_b, tr, mask_angle):
    try:
        X_iter, Y_iter, Z_iter = X_b, Y_b, Z_b 
        lat_b, lon_b, alt_b = ecef_a_geodesicas(X_b, Y_b, Z_b)
        alpha, beta = nav.get('_iono', {'alpha': [0]*4, 'beta': [0]*4})['alpha'], nav.get('_iono', {'alpha': [0]*4, 'beta': [0]*4})['beta']
        
        sat_positions = {}
        for s, d in sd_epoca.items():
            if s == '_meta' or d['sd_P'] is None: continue 
            tau = d['pr_r'] / C_LIGHT
            sp = calcular_posicion_satelite_wgs84(seleccionar_efemeride_optima(nav.get(s), tr-tau), tr-tau, tau, s[0])
            if sp:
                el_r, az_r = calcular_topocentricas(sp[0], sp[1], sp[2], X_iter, Y_iter, Z_iter)
                if el_r >= mask_angle: sat_positions[s] = {'sp': sp, 'el': el_r, 'az': az_r, 'sd_P': d['sd_P'], 'snr': d.get('snr', 30.0)}
        
        if len(sat_positions) < 4: return None, "FAILED"
        
        sat_list_full = list(sat_positions.keys())
        constellations = set([s[0] for s in sat_list_full])
        ref_sats, sat_list = {}, []
        for c in constellations:
            c_sats = [s for s in sat_list_full if s[0] == c]
            if len(c_sats) >= 2:
                ref_sats[c] = max(c_sats, key=lambda k: sat_positions[k]['el'])
                c_sats.remove(ref_sats[c])
                sat_list.extend(c_sats)
        if len(sat_list) < 3: return None, "FAILED" 
        
        def calc_rho(sp, X, Y, Z, lat, lon, alt, el, az):
            dist = math.sqrt((sp[0]-X)**2 + (sp[1]-Y)**2 + (sp[2]-Z)**2)
            return dist + calcular_saastamoinen(lat, alt, el), calcular_klobuchar(lat, lon, el, az, tr, alpha, beta), dist

        prev_residuals = [0.0] * len(sat_list)

        for iteracion in range(8):
            lat_it, lon_it, alt_it = ecef_a_geodesicas(X_iter, Y_iter, Z_iter)
            H, L, W_diag = [], [], []
            
            ref_calcs = {}
            for c, r_sat in ref_sats.items():
                r_data = sat_positions[r_sat]
                rho_ref_r_base, iono_ref_r, dist_ref_r = calc_rho(r_data['sp'], X_iter, Y_iter, Z_iter, lat_it, lon_it, alt_it, r_data['el'], r_data['az'])
                el_ref_b, az_ref_b = calcular_topocentricas(r_data['sp'][0], r_data['sp'][1], r_data['sp'][2], X_b, Y_b, Z_b)
                rho_ref_b_base, iono_ref_b, _ = calc_rho(r_data['sp'], X_b, Y_b, Z_b, lat_b, lon_b, alt_b, el_ref_b, az_ref_b)
                ref_calcs[c] = {'dist_ref_r': dist_ref_r, 'SD_P_calc_ref': (rho_ref_r_base + iono_ref_r) - (rho_ref_b_base + iono_ref_b), 'sp': r_data['sp'], 'el': r_data['el'], 'snr': r_data.get('snr', 30.0), 'sd_P': r_data['sd_P']}
            
            res_idx = 0
            for i, s in enumerate(sat_list):
                c = s[0]; data = sat_positions[s]; rc = ref_calcs[c]
                rho_i_r_base, iono_i_r, dist_i_r = calc_rho(data['sp'], X_iter, Y_iter, Z_iter, lat_it, lon_it, alt_it, data['el'], data['az'])
                el_i_b, az_i_b = calcular_topocentricas(data['sp'][0], data['sp'][1], data['sp'][2], X_b, Y_b, Z_b)
                rho_i_b_base, iono_i_b, _ = calc_rho(data['sp'], X_b, Y_b, Z_b, lat_b, lon_b, alt_b, el_i_b, az_i_b)
                
                SD_P_calc_i = (rho_i_r_base + iono_i_r) - (rho_i_b_base + iono_i_b)
                DD_P_calc = SD_P_calc_i - rc['SD_P_calc_ref']
                dx_geom = [-(data['sp'][0] - X_iter) / dist_i_r - (-(rc['sp'][0] - X_iter) / rc['dist_ref_r']), -(data['sp'][1] - Y_iter) / dist_i_r - (-(rc['sp'][1] - Y_iter) / rc['dist_ref_r']), -(data['sp'][2] - Z_iter) / dist_i_r - (-(rc['sp'][2] - Z_iter) / rc['dist_ref_r'])]
                
                w_i_ref = (math.sin(math.radians(data['el']))**2 * (10.0 ** (data.get('snr', 30.0) / 10.0)) * math.sin(math.radians(rc['el']))**2 * (10.0 ** (rc['snr'] / 10.0))) / max(1.0, (math.sin(math.radians(data['el']))**2 * (10.0 ** (data.get('snr', 30.0) / 10.0))) + (math.sin(math.radians(rc['el']))**2 * (10.0 ** (rc['snr'] / 10.0))))
                
                L.append([data['sd_P'] - rc['sd_P'] - DD_P_calc]); H.append(dx_geom)
                W_diag.append(w_i_ref * 1.0 if iteracion == 0 else w_i_ref * 1.0 / max(1.0, abs(prev_residuals[res_idx]) / 2.0))
                res_idx += 1

            H_T = transpose_matrix(H)
            if not H_T or not W_diag: return None, "FAILED" 
            H_T_W = [[H_T[r][idx] * W_diag[idx] for idx in range(len(W_diag))] for r in range(len(H_T))]
            N_mat = matmul(H_T_W, H)
            for r in range(len(N_mat)): N_mat[r][r] += abs(N_mat[r][r]) * 1e-6 + 1e-6
                
            Q = invert_matrix_nxn(N_mat)
            if not Q: return None, "FAILED"
            Delta_X = matmul(Q, matmul(H_T_W, L))
            if not Delta_X or len(Delta_X) < 3 or not Delta_X[0]: return None, "FAILED" 

            X_iter += Delta_X[0][0]; Y_iter += Delta_X[1][0]; Z_iter += Delta_X[2][0]
                
            prev_residuals = []
            for r in range(len(H)): prev_residuals.append(sum(H[r][idx] * Delta_X[idx][0] for idx in range(len(H[0]))) - L[r][0])
            if max(abs(Delta_X[0][0]), abs(Delta_X[1][0]), abs(Delta_X[2][0])) < 1e-3: return (X_iter, Y_iter, Z_iter), "FLOAT"
        return (X_iter, Y_iter, Z_iter), "FLOAT"
    except Exception as e: return None, f"FAILED_EXCEPTION:_{str(e)}"

def estadistica_desacoplada(coordenadas, conf_plani, conf_alti, err_hor_max, err_ver_max):
    if not coordenadas: return None, None, None, 0, 0, 0, 0, 0.0
    N_list, E_list, Z_list = [c[0] for c in coordenadas], [c[1] for c in coordenadas], [c[2] for c in coordenadas]
    def get_median(lst):
        s = sorted(lst); n = len(s)
        return s[n//2] if n % 2 == 1 else (s[n//2 - 1] + s[n//2]) / 2.0

    med_N, med_E, med_Z = get_median(N_list), get_median(E_list), get_median(Z_list)
    valid_coords = [c for c in coordenadas if not ((err_hor_max > 0.0 and math.hypot(c[0] - med_N, c[1] - med_E) > err_hor_max) or (err_ver_max > 0.0 and abs(c[2] - med_Z) > err_ver_max))]

    if not valid_coords: return None, None, None, 0, 0, 0, 0, 0.0
    
    N_v, E_v, Z_v = [c[0] for c in valid_coords], [c[1] for c in valid_coords], [c[2] for c in valid_coords]
    def calc_mean_std(arr):
        n = len(arr); m = sum(arr) / n
        return m, (math.sqrt(sum((x - m)**2 for x in arr) / n) if n > 1 else 0.0)

    N_m, N_s = calc_mean_std(N_v); E_m, E_s = calc_mean_std(E_v); Z_m, Z_s = calc_mean_std(Z_v)
    N_f = [x for x in N_v if abs(x - N_m) <= conf_plani * N_s] if N_s > 0 else N_v
    E_f = [x for x in E_v if abs(x - E_m) <= conf_plani * E_s] if E_s > 0 else E_v
    Z_f = [x for x in Z_v if abs(x - Z_m) <= conf_alti * Z_s] if Z_s > 0 else Z_v

    return sum(N_f)/max(1, len(N_f)), sum(E_f)/max(1, len(E_f)), sum(Z_f)/max(1, len(Z_f)), N_s, E_s, Z_s, min(len(N_f), len(E_f), len(Z_f)), (len([c[3] for c in valid_coords if c[3] == "FIXED"]) / len(valid_coords)) * 100

def generar_informe_ascii(p_dict):
    return f"""
========================================================================
             INFORME DE PROCESAMIENTO GNSSJP PRO 
========================================================================

[*] RESULTADO DE MEDICIÓN ABSOLUTA (FLOAT (DGPS))
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

  * COORDENADA CALCULADA (AJUSTE IRLS DGPS FLOAT (DGPS)):
      Norte : {p_dict['r_n_calc']:.3f} m
      Este  : {p_dict['r_e_calc']:.3f} m
      Cota  : {p_dict['r_z_calc']:.3f} m
========================================================================
"""

# =====================================================================
# RUTAS FLASK (ZERO-STATE / STATELESS)
# =====================================================================
@app.route('/')
def index(): return send_file('index.html')

@app.route('/tab1_homogenizar', methods=['POST'])
def tab1_homogenizar():
    url_b, url_r = request.form.get('url_base'), request.form.get('url_rover')
    if not url_b or not url_r: return Response("> [ERROR CRÍTICO] Faltan URLs de Google Drive.\n", mimetype='text/plain')
    p_b_raw, p_r_raw = os.path.join(UPLOAD_FOLDER, 'base_raw.obs'), os.path.join(UPLOAD_FOLDER, 'rover_calibracion_raw.obs')

    def procesar():
        try:
            yield "> [SISTEMA] Descargando Archivos desde Google Drive (Stateless)...\n"
            descargar_gdrive_publico(url_b, p_b_raw)
            descargar_gdrive_publico(url_r, p_r_raw)
            yield f"> [SISTEMA] Iniciando Etapa 1: Emparejamiento...\n"
            base_raw_dict, rover_raw_dict = parse_rinex_obs_completo(p_b_raw), parse_rinex_obs_completo(p_r_raw)
            
            base_sinc, c, base_tows = {}, 0, sorted(list(base_raw_dict.keys()))
            for tr in sorted(list(rover_raw_dict.keys())):
                c += 1
                if c % max(1, len(rover_raw_dict) // 10) == 0: yield f"[PROGRESO] Cotejando épocas sin distorsión... {int((c / len(rover_raw_dict)) * 100)}%\n"
                if not base_tows: continue
                idx = min(range(len(base_tows)), key=lambda i: abs(base_tows[i] - tr))
                if abs(base_tows[idx] - tr) <= 0.05:
                    base_sinc[tr] = base_raw_dict[base_tows[idx]].copy()
                    base_sinc[tr]['_meta'] = rover_raw_dict[tr]['_meta']
            
            if not base_sinc: yield "\n> [ERROR FATAL] Cero épocas en común. Revisar rango horario."; return
            yield f"\n========================================================================\n    AUDITORÍA FORENSE DE EMPAREJAMIENTO DE ÉPOCAS\n========================================================================\n[1] PARÁMETROS DE CONTROL (BASE) : {extraer_gdrive_id(url_b)}.obs\n[2] PARÁMETROS DEL MÓVIL (ROVER) : {extraer_gdrive_id(url_r)}.obs\n[3] MATRIZ RESULTANTE (ESTRICTA, SIN INTERPOLACIÓN)\n  [-] Épocas Útiles Sincronizadas: {len(base_sinc)}\n  [-] Tasa de Éxito sobre Rover  : {(len(base_sinc) / max(1, len(rover_raw_dict))) * 100:.1f}%\n========================================================================\n\n[SUCCESS]"
        except Exception as e: yield f"\n> [ERROR] Falla estructural: {str(e)}"
    return Response(procesar(), mimetype='text/plain')

@app.route('/tab2_efemerides', methods=['POST'])
def tab2_efemerides():
    def procesar():
        yield "> [SISTEMA] Test de conexión IGS/BKG...\n"
        yield "> [SISTEMA] En arquitectura Zero-State, las efemérides se descargarán automáticamente durante el cálculo.\n\n[SUCCESS]"
    return Response(procesar(), mimetype='text/plain')

@app.route('/tab3_calibrar', methods=['POST'])
def tab3_calibrar():
    url_b, url_r = request.form.get('url_base'), request.form.get('url_rover')
    utm_n, utm_e, utm_c = safe_f(request.form.get('utm_norte')), safe_f(request.form.get('utm_este')), safe_f(request.form.get('utm_cota'))
    utm_h, utm_hem = safe_i(request.form.get('utm_huso')), request.form.get('utm_hemisferio', 'N')
    utm_n_r, utm_e_r, utm_c_r = safe_f(request.form.get('utm_norte_r')), safe_f(request.form.get('utm_este_r')), safe_f(request.form.get('utm_cota_r'))

    def procesar():
        try:
            yield "> [SISTEMA] Iniciando Búsqueda Determinista (Arquitectura Zero-State)...\n"
            if 0.0 in [utm_e, utm_n, utm_n_r, utm_e_r]: yield "> [ERROR] Coordenadas incompletas.\n"; return
            if not url_b or not url_r: yield "> [ERROR FATAL] URLs de GDrive no proporcionadas.\n"; return
            
            p_b_raw, p_r_raw = os.path.join(UPLOAD_FOLDER, 'base_raw.obs'), os.path.join(UPLOAD_FOLDER, 'rover_calib.obs')
            yield "[PROGRESO] Descargando archivos crudos al vuelo...\n"
            descargar_gdrive_publico(url_b, p_b_raw)
            descargar_gdrive_publico(url_r, p_r_raw)
            
            obs_b_raw_crudo, obs_r_raw_crudo = parse_rinex_obs_completo(p_b_raw), parse_rinex_obs_completo(p_r_raw)
            ft = obtener_fecha_obs(p_b_raw)
            if not ft: yield "> [ERROR FATAL] Imposible extraer la fecha de la Base.\n"; return
            
            nav_path = None
            for tipo, log in descargar_efemerides_brdc_stream(ft[0], ft[1], ft[2], ft[3]):
                if tipo == "INFO": yield f"  {log}"
                elif tipo == "SUCCESS": nav_path = log
                elif tipo == "ERROR": yield f"> [ERROR CRÍTICO RED] {log}\n"; return
            nav = parse_rinex_nav_real(nav_path)
            
            yield "[PROGRESO] Sincronización Estricta (< 0.05s) en RAM...\n"
            base_sinc_crudo, rover_tows, base_tows = {}, sorted(list(obs_r_raw_crudo.keys())), sorted(list(obs_b_raw_crudo.keys()))
            for tr in rover_tows:
                if not base_tows: continue
                idx = min(range(len(base_tows)), key=lambda i: abs(base_tows[i] - tr))
                if abs(base_tows[idx] - tr) <= 0.05:
                    base_sinc_crudo[tr] = obs_b_raw_crudo[base_tows[idx]].copy()
                    base_sinc_crudo[tr]['_meta'] = obs_r_raw_crudo[tr]['_meta']

            yield "[PROGRESO] Simulando caída geométrica de precisión en RAM...\n"
            sd_suavizada = aislar_diferencias_simples_ppk(simular_escritura_disco_js(base_sinc_crudo), simular_escritura_disco_js(obs_r_raw_crudo))
            if not sd_suavizada: yield "> [ERROR] No hay épocas sincronizadas válidas.\n"; return

            t_sample = list(sd_suavizada.keys())
            lat_b, lon_b, _ = utm_a_geodesicas(utm_e, utm_n, utm_h, utm_hem)
            X_b, Y_b, Z_b = geodesicas_a_ecef(lat_b, lon_b, utm_c)

            yield "[PROGRESO] Fase 1: Extrayendo Errores Máximos Permitidos...\n"
            coords_raw = []
            for t in t_sample:
                sem, status = calcular_dd_ppk_lambda_epoca(sd_suavizada[t], nav, X_b, Y_b, Z_b, t, 10.0)
                if sem:
                    la, lo, al = ecef_a_geodesicas(sem[0], sem[1], sem[2])
                    nt, et = geodesicas_a_utm(la, lo, utm_h)
                    coords_raw.append((nt, et, al))
            
            if not coords_raw: yield "> [ERROR] Nube de puntos bruta colapsada.\n"; return
                
            deltas_h = sorted([math.hypot(c[0] - utm_n_r, c[1] - utm_e_r) for c in coords_raw])
            deltas_v = sorted([abs(c[2] - utm_c_r) for c in coords_raw])
            best_eh, best_ev = max(0.01, float(deltas_h[max(1, len(deltas_h) // 10)])), max(0.01, float(deltas_v[max(1, len(deltas_v) // 10)]))
            
            yield f"  [*] Límite Horizontal Inyectado: {best_eh:.14f} m\n  [*] Límite Vertical Inyectado: {best_ev:.14f} m\n\n"
            yield "[PROGRESO] Fase 2: Malla Determinista para Parámetros (M, Cp, Ca)...\n"
            
            best_rmse, best_params = float('inf'), {}
            m_center, m_span, cp_center, cp_span, ca_center, ca_span = 10.0, 5.0, 2.0, 1.5, 2.0, 1.5
            
            for nivel in range(8):
                yield f"  [+] Refinando espacio de búsqueda (Zoom {nivel+1}/8)...\n"
                m_grid = [max(5.0, min(15.0, x)) for x in [m_center - m_span, m_center, m_center + m_span]]
                cp_grid = [max(0.1, min(5.0, x)) for x in [cp_center - cp_span, cp_center, cp_center + cp_span]]
                ca_grid = [max(0.1, min(5.0, x)) for x in [ca_center - ca_span, ca_center, ca_center + ca_span]]
                
                nivel_best_rmse, nivel_best_m, nivel_best_cp, nivel_best_ca = float('inf'), m_center, cp_center, ca_center
                
                for m in set(m_grid):
                    coords = []
                    for t in t_sample:
                        sem, status = calcular_dd_ppk_lambda_epoca(sd_suavizada[t], nav, X_b, Y_b, Z_b, t, m)
                        if sem:
                            la, lo, al = ecef_a_geodesicas(sem[0], sem[1], sem[2])
                            nt, et = geodesicas_a_utm(la, lo, utm_h)
                            coords.append((nt, et, al, status))
                    if not coords: continue
                    
                    for cp in set(cp_grid):
                        for ca in set(ca_grid):
                            res = estadistica_desacoplada(coords, cp, ca, best_eh, best_ev)
                            if res[0] is None: continue
                            rmse_3d = math.sqrt((res[0] - utm_n_r)**2 + (res[1] - utm_e_r)**2 + (res[2] - utm_c_r)**2)
                            if rmse_3d < nivel_best_rmse:
                                nivel_best_rmse, nivel_best_m, nivel_best_cp, nivel_best_ca = rmse_3d, m, cp, ca
                                best_rmse, best_params = rmse_3d, {'mask': m, 'cp': cp, 'ca': ca, 'eh': best_eh, 'ev': best_ev, 'rmse': rmse_3d, 'ret': res[6], 'dn': res[0] - utm_n_r, 'de': res[1] - utm_e_r, 'dz': res[2] - utm_c_r}
                
                m_center, m_span = nivel_best_m, m_span / 2.0
                cp_center, cp_span = nivel_best_cp, cp_span / 2.0
                ca_center, ca_span = nivel_best_ca, ca_span / 2.0
            
            if best_rmse != float('inf'):
                yield f"\n========================================================\n      [INFORME] PARÁMETROS ÓPTIMOS (CALIBRACIÓN OR)\n========================================================\n  [-] Máscara Elevación (°): {best_params['mask']:.14f}\n  [-] Filtro Sigma Plan (cp): {best_params['cp']:.14f}\n  [-] Filtro Sigma Alt (ca): {best_params['ca']:.14f}\n  [-] Error Permitido Horizontal (m): {best_params['eh']:.14f}\n  [-] Error Permitido Vertical (m): {best_params['ev']:.14f}\n--------------------------------------------------------\n  [*] RMSE Global 3D al Punto: {best_params['rmse']:.4f} m\n  [*] Deltas Residuales -> N: {best_params['dn']:.3f}m, E: {best_params['de']:.3f}m, Z: {best_params['dz']:.3f}m\n  [*] Épocas Retenidas: {best_params['ret']}\n========================================================\n\n[SUCCESS]"
            else: yield "\n> [ERROR] El modelo determinista no convergió. Filtros demasiado agresivos.\n"
        except Exception as e: yield f"\n> [ERROR FATAL] {str(e)}"
    return Response(procesar(), mimetype='text/plain')

@app.route('/tab4_procesar', methods=['POST'])
def tab4_procesar():
    url_b, url_r_nuevo = request.form.get('url_base'), request.form.get('url_rover_nuevo')
    utm_n, utm_e, utm_c = safe_f(request.form.get('utm_norte')), safe_f(request.form.get('utm_este')), safe_f(request.form.get('utm_cota'))
    utm_h, utm_hem = safe_i(request.form.get('utm_huso')), request.form.get('utm_hemisferio', 'N')
    h_b, h_r = safe_f(request.form.get('altura_base')), safe_f(request.form.get('altura_rover'))
    p_mask, p_cp, p_ca = safe_f(request.form.get('param_mask'), 10.0), safe_f(request.form.get('param_cp'), 2.5), safe_f(request.form.get('param_ca'), 1.5)
    err_hor_max, err_ver_max = safe_f(request.form.get('err_hor_max'), 0.5), safe_f(request.form.get('err_ver_max'), 0.5)

    def procesar():
        try:
            yield "> [SISTEMA] Iniciando Procesamiento DGPS Monolítico...\n"
            if utm_e == 0.0 or utm_n == 0.0: yield "> [ERROR] Coordenadas Base incompletas.\n"; return
            if not url_b or not url_r_nuevo: yield "> [ERROR FATAL] URLs de GDrive no proporcionadas.\n"; return

            p_b_raw, p_r_nuevo = os.path.join(UPLOAD_FOLDER, 'base_raw.obs'), os.path.join(UPLOAD_FOLDER, 'rover_nuevo_raw.obs')
            yield "[PROGRESO] Descargando archivos crudos al vuelo...\n"
            descargar_gdrive_publico(url_b, p_b_raw)
            descargar_gdrive_publico(url_r_nuevo, p_r_nuevo)

            obs_b_raw_crudo, obs_r_raw_crudo = parse_rinex_obs_completo(p_b_raw), parse_rinex_obs_completo(p_r_nuevo) 
            ft = obtener_fecha_obs(p_b_raw)
            nav_path, nav_filename = None, "auto_nav.nav"
            for tipo, log in descargar_efemerides_brdc_stream(ft[0], ft[1], ft[2], ft[3]):
                if tipo == "INFO": yield f"  {log}"
                elif tipo == "SUCCESS": nav_path, nav_filename = log, os.path.basename(log)
            nav = parse_rinex_nav_real(nav_path)
            
            yield "[PROGRESO] Emparejamiento Temporal Dinámico (< 0.05s)...\n"
            base_sinc, rover_tows, base_tows = {}, sorted(list(obs_r_raw_crudo.keys())), sorted(list(obs_b_raw_crudo.keys()))
            for tr in rover_tows:
                if not base_tows: continue
                idx = min(range(len(base_tows)), key=lambda i: abs(base_tows[i] - tr))
                if abs(base_tows[idx] - tr) <= 0.05:
                    base_sinc[tr] = obs_b_raw_crudo[base_tows[idx]].copy()
                    base_sinc[tr]['_meta'] = obs_r_raw_crudo[tr]['_meta']
            
            yield "[PROGRESO] Extrayendo Observables DGPS (Datos Crudos sin truncar)...\n"
            sd_suavizada = aislar_diferencias_simples_ppk(base_sinc, obs_r_raw_crudo)
            if not sd_suavizada: yield "\n> [ERROR] No hay épocas sincronizadas válidas.\n"; return

            lat_b, lon_b, _ = utm_a_geodesicas(utm_e, utm_n, utm_h, utm_hem)
            X_b, Y_b, Z_b = geodesicas_a_ecef(lat_b, lon_b, utm_c + h_b)

            coords, t_eps, c = [], len(sd_suavizada), 0
            for t in sd_suavizada:
                c += 1
                if c % max(1, t_eps // 10) == 0: yield f"[PROGRESO] Resolviendo Ecuaciones Matriciales DGPS... {int((c / t_eps) * 100)}%\n"
                sem, status = calcular_dd_ppk_lambda_epoca(sd_suavizada[t], nav, X_b, Y_b, Z_b, t, p_mask)
                if sem:
                    la, lo, al = ecef_a_geodesicas(sem[0], sem[1], sem[2])
                    nt, et = geodesicas_a_utm(la, lo, utm_h)
                    coords.append((nt, et, al, status))

            if not coords: yield "\n> [ERROR] Fracaso algorítmico total en Inversión NxN.\n"; return
            
            res = estadistica_desacoplada(coords, p_cp, p_ca, err_hor_max, err_ver_max)
            if res[0] is None: yield "\n> [ERROR] Operación Abortada: El 100% de las épocas superan el Error Máximo.\n"; return
                
            p_dict = {
                'mask': p_mask, 'cp': p_cp, 'ca': p_ca, 'err_h': err_hor_max, 'err_v': err_ver_max,
                'nf': res[0], 'ef': res[1], 'zf': res[2] - h_r, 
                'ret': res[6], 'total': len(coords), 'std_n': res[3], 'std_e': res[4], 'std_z': res[5], 'ez': res[5], 'fix_r': res[7],
                'base_file': extraer_gdrive_id(url_b) + ".obs", 'rover_file': extraer_gdrive_id(url_r_nuevo) + ".obs", 'nav_file': nav_filename,
                'b_n': utm_n, 'b_e': utm_e, 'b_z': utm_c,
                'r_n_calc': res[0], 'r_e_calc': res[1], 'r_z_calc': res[2] - h_r
            }
            
            yield "[PROGRESO] Ajuste DGPS Finalizado.\n"
            yield generar_informe_ascii(p_dict)
            yield "\n[SUCCESS]"
        except Exception as e: yield f"\n> [ERROR FATAL] {str(e)}"
    return Response(procesar(), mimetype='text/plain')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9000, debug=True)
