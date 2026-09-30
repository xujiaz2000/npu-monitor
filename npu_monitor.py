# -*- coding: utf-8 -*-
"""
NPU 服务器占用监控看板 (纯标准库: tkinter + ssh, 无第三方依赖)

功能:
  * 并行轮询多台昇腾 Atlas 服务器 (每台独立线程), 解析 `npu-smi info`
  * 简洁可视化: 状态摘要 + 每卡占用小方块 + Core利用率迷你趋势, 不是输出原始报表
  * 服务器管理: 添加 / 编辑 / 删除 / 测试连接 (配置持久化 npu_monitor.json)
  * 桌面弹窗提醒: 空闲 / 占用恢复 / 连接失败 (可对单台静默 30 分钟)
  * 阈值可配置: Core利用率 / HBM显存 / 连续确认轮数 / 重复提醒间隔

用法:
  python npu_monitor.py              # 正常监控
  python npu_monitor.py --demo       # 演示模式 (合成数据, 不连真实服务器)
  python npu_monitor.py --selftest   # 解析自检 (用目录内 probe_*.txt), 不开界面
  python npu_monitor.py --smoke      # 构建 UI 后 2.5 秒自动退出 (自检)
"""

import json
import math
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import messagebox, ttk

IS_WIN = os.name == 'nt'
NO_WINDOW = 0x08000000 if IS_WIN else 0          # CREATE_NO_WINDOW

APP_DIR  = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(APP_DIR, 'npu_monitor.json')
LOG_PATH = os.path.join(APP_DIR, 'monitor_events.log')

APP_TITLE = 'NPU 占用监控'
SSH_BIN   = shutil.which('ssh') or 'ssh'
VER       = 'v1.0'

# ---------------------------------------------------------------------------
# 深色配色 / 字体
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Win11 深色 (Mica 风) 配色 / 字体
# ---------------------------------------------------------------------------
BG    = '#202020'      # 窗口底 (Mica 深灰)
CARD  = '#2b2b2b'      # 卡片
CARD2 = '#383838'      # 输入框 / 次级元素
LINE  = '#454545'      # 分隔 / 描边
TXT   = '#f3f3f3'
DIM   = '#a8a8a8'
ACC   = '#4cc2ff'      # Win11 深色模式强调色
OKC   = '#6ccb5f'      # 空闲 / 成功
WARNC = '#ffb84d'      # 占用
ERRC  = '#ff6b6b'      # 故障 / 高温

T_IDLE_BG, T_BUSY_BG, T_WARN_BG = '#24301f', '#322a18', '#323018'
T_WARN_BORDER = '#e5c15c'
T_TRACK = '#1a1a1a'    # 方块内进度条底槽

F_T    = ('Microsoft YaHei UI', 13, 'bold')
F_HD   = ('Microsoft YaHei UI', 10)
F_HDB  = ('Microsoft YaHei UI', 11, 'bold')
F_SM   = ('Microsoft YaHei UI', 10)
F_XS   = ('Microsoft YaHei UI', 9)
F_NUM  = ('Consolas', 10)
F_TILE = ('Consolas', 11, 'bold')
F_TILES= ('Consolas', 9)

CHIP_W = CHIP_H = CHIP_DX = CHIP_DY = CHIP_PAD = 0
SPARK_W = SPARK_H = 0


def apply_ui_scale(s):
    """按 DPI 缩放画布像素常量 (字体由 tk 随系统 DPI 自动缩放, 像素常量必须同步)"""
    global CHIP_W, CHIP_H, CHIP_DX, CHIP_DY, CHIP_PAD, SPARK_W, SPARK_H
    s = max(1.0, float(s))
    CHIP_W   = int(round(82 * s))
    CHIP_H   = int(round(66 * s))
    CHIP_DX  = CHIP_W + int(round(8 * s))
    CHIP_DY  = CHIP_H + int(round(8 * s))
    CHIP_PAD = int(round(10 * s))
    SPARK_W  = int(round(120 * s))
    SPARK_H  = int(round(26 * s))


apply_ui_scale(1.0)

# ---------------------------------------------------------------------------
# 默认配置 (首次运行写入 npu_monitor.json)
# ---------------------------------------------------------------------------
DEFAULT_CFG = {
    'interval': 15,
    'connect_timeout': 5,
    'exec_timeout': 25,
    'sound': True,
    'alert_idle': True,
    'alert_recover': False,
    'alert_fail': True,
    'renotify_min': 30,        # 空闲持续提醒间隔(分钟), 0=不重复
    'defaults': {
        'idle_core': 5,        # 判占用: 任一卡 Core > 5%
        'idle_hbm_gb': 8,      # 或 HBM 已用 > 8 GB
        'idle_cycles': 3,      # 连续 3 次采样空闲才提醒
    },
    'servers': [],             # 默认不带服务器: 首次运行自动读取 VSCode/OpenSSH 的 SSH 配置
    'ui': {'geometry': ''},
}

# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------
def load_cfg():
    first = not os.path.isfile(CFG_PATH)
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    if not first:
        try:
            with open(CFG_PATH, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            for k, v in saved.items():
                if k == 'defaults' and isinstance(v, dict):
                    cfg['defaults'].update(v)
                elif k == 'ui' and isinstance(v, dict):
                    cfg['ui'].update(v)
                else:
                    cfg[k] = v
        except Exception:
            pass
    servers = cfg.get('servers')
    if not isinstance(servers, list):
        servers = []
    if not servers and first:
        servers = vscode_ssh_hosts()   # 首次运行: 读取 VSCode/OpenSSH 的 SSH 配置
    cfg['servers'] = servers
    for i, s in enumerate(cfg['servers']):
        s.setdefault('id', 's%d' % (i + 1))
        s.setdefault('label', '')
        s.setdefault('host', '')
        s.setdefault('user', '')       # 空 = 使用 ssh 默认用户
        s.setdefault('port', 22)
        s.setdefault('enabled', True)
    for k in ('interval', 'connect_timeout', 'exec_timeout', 'renotify_min'):
        try:
            cfg[k] = int(cfg.get(k, DEFAULT_CFG[k]))
        except (TypeError, ValueError):
            cfg[k] = DEFAULT_CFG[k]
    return cfg


# ---------------------------------------------------------------------------
# 读取 VSCode Remote-SSH / 系统 OpenSSH 的 SSH 配置 (首次运行自动导入)
# ---------------------------------------------------------------------------
def parse_ssh_config(path):
    """解析 ssh config 文件, 返回 [{label, host, user, port}]; 跳过通配/排除项"""
    rows, cur, labels = [], None, set()
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split(None, 1)
                if len(parts) < 2:
                    continue
                key, val = parts[0].lower(), parts[1].strip()
                if key == 'host':
                    if cur and cur.get('host') and cur['label'] not in labels:
                        rows.append(cur)
                        labels.add(cur['label'])
                    aliases = [a for a in val.split()
                               if '*' not in a and '!' not in a and '?' not in a]
                    cur = {'label': aliases[0], 'host': '', 'user': '',
                           'port': 22} if aliases else None
                elif cur is not None:
                    if key == 'hostname':
                        cur['host'] = val.split()[0]
                    elif key == 'user':
                        cur['user'] = val.split()[0]
                    elif key == 'port':
                        try:
                            cur['port'] = int(val.split()[0])
                        except ValueError:
                            pass
    except Exception:
        pass
    if cur and cur.get('host') and cur['label'] not in labels:
        rows.append(cur)
    return rows[:24]                   # 上限保护, 避免超大配置拖慢启动


def vscode_ssh_hosts():
    """优先读取 VSCode 指定的 remote.SSH.configFile, 其次 ~/.ssh/config"""
    candidates = []
    try:
        sj = os.path.join(os.environ.get('APPDATA', ''),
                          'Code', 'User', 'settings.json')
        if os.path.isfile(sj):
            with open(sj, 'r', encoding='utf-8', errors='replace') as f:
                txt = f.read()
            m = re.search(r'remote\.SSH\.configFile["\']?\s*:\s*["\']([^"\']+)', txt)
            if m:
                p = m.group(1).replace('\\\\', '\\')
                if p.startswith('~'):
                    p = os.path.join(os.path.expanduser('~'), p[1:])
                candidates.append(p)
    except Exception:
        pass
    candidates.append(os.path.join(os.path.expanduser('~'), '.ssh', 'config'))
    for p in candidates:
        if p and os.path.isfile(p):
            rows = parse_ssh_config(p)
            if rows:
                return rows
    return []


def save_cfg(cfg):
    try:
        tmp = CFG_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG_PATH)
    except Exception:
        pass


def log_event(kind, server, msg):
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write('%s | %-7s | %s | %s\n' % (
                time.strftime('%Y-%m-%d %H:%M:%S'), kind, server, msg.replace('\n', ' / ')))
    except Exception:
        pass


def ov(row, g, key):
    """阈值取值: 服务器覆盖(非空字符串/数字) > 全局默认"""
    v = row.get(key) if row else None
    try:
        if v is not None and str(v).strip() != '':
            return float(v)
    except (TypeError, ValueError):
        pass
    try:
        return float(g['defaults'][key])
    except (KeyError, TypeError, ValueError):
        if key == 'idle_core':
            return 5.0
        if key == 'idle_hbm_gb':
            return 8.0
        return 3.0


def fmt_gb(mb, nd=1):
    if mb is None:
        return '?'
    return ('%.*f' % (nd, mb / 1024.0))


def fmt_dur(sec):
    if sec is None or sec < 0:
        return '0s'
    sec = int(sec)
    if sec < 60:
        return '%ds' % sec
    if sec < 3600:
        return '%d分%02ds' % (sec // 60, sec % 60)
    return '%d时%02d分' % (sec // 3600, (sec % 3600) // 60)


def fmt_pct(v):
    return '—' if v is None else '%.1f%%' % v


def fit_line(s, cols=30):
    """按显示宽度截断为单行 (中文/全角按 2 列计), 超出部分以 … 结尾"""
    w, out = 0, []
    for ch in s:
        cw = 2 if ord(ch) > 0x2E80 else 1
        if w + cw > cols:
            out.append('…')
            break
        out.append(ch)
        w += cw
    return ''.join(out)


def hhmm(ts):
    return time.strftime('%H:%M:%S', time.localtime(ts))


# ---------------------------------------------------------------------------
# Win11 视觉辅助: 深色标题栏 / 无边框窗口圆角 / 按钮悬停
# ---------------------------------------------------------------------------
def win11_titlebar(win):
    """把窗口标题栏切到深色 (DWMWA_USE_IMMERSIVE_DARK_MODE)"""
    if not IS_WIN:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        for attr in (20, 19):          # 20 = 新版, 19 = 旧版 Win10
            v = ctypes.c_int(1)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(v), 4) == 0:
                break
    except Exception:
        pass


def round_window(win, radius):
    """给无边框窗口做圆角裁剪 (Win11 风格)"""
    if not IS_WIN or radius <= 0:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        w = max(2, win.winfo_width())
        h = max(2, win.winfo_height())
        rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, int(radius), int(radius))
        user32.SetWindowRgn(hwnd, rgn, True)
    except Exception:
        pass


def hover_btn(b, normal_bg, hover_bg, normal_fg=None, hover_fg=None):
    """Tk 按钮的悬停变色"""
    b.bind('<Enter>', lambda e: b.configure(
        bg=hover_bg, fg=hover_fg if hover_fg else b.cget('fg')))
    b.bind('<Leave>', lambda e: b.configure(
        bg=normal_bg, fg=normal_fg if normal_fg else b.cget('fg')))


def virtual_screen():
    """整个虚拟桌面 (所有显示器拼接) 的边界: (left, top, right, bottom)"""
    if IS_WIN:
        try:
            import ctypes
            u = ctypes.windll.user32
            l, t = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
            w, h = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
            return (l, t, l + w, t + h)
        except Exception:
            pass
    return (0, 0, 0, 0)

# ---------------------------------------------------------------------------
# npu-smi 解析 (不依赖列宽, 兼容 25.x / 26.x 实测输出)
#   每张卡由两行组成:
#     | 卡号  Ascend910  | OK  | 功率 温度 页表F1/F2           |
#     | 组号  物理ID     | PCIe| AI%  内存A/B  HBM内存C/D     |
#   注意: 芯片行第一列是组内序号(0/1), 第二列才是全机 Phy-ID(0..15)。
# ---------------------------------------------------------------------------
RE_ROW_CHIP = re.compile(r'^\|\s*(\d+)\s+(\d+)\s*\|\s*([0-9A-Fa-f:.]*[.:][0-9A-Fa-f:.]*)\s*\|\s*(.*?)\s*\|\s*$')
RE_ROW_MAIN = re.compile(r'^\|\s*(\d+)\s+(\S+)\s*\|\s*([^|]+?)\s*\|\s*(.*?)\s*\|\s*$')
RE_FRAC = re.compile(r'(\d+)\s*/\s*(\d+)')
RE_NUM  = re.compile(r'-?\d+(?:\.\d+)?')


def _nums_without_frac(s):
    rest = RE_FRAC.sub(' ', s)
    return [float(x) for x in RE_NUM.findall(rest)]


def parse_npu_smi(text):
    chips = []
    last_main = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.startswith('|'):
            continue

        m = RE_ROW_CHIP.match(line)
        if m:
            grp, phy, bus, c4 = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
            frs = [(int(a), int(b)) for a, b in RE_FRAC.findall(c4)]
            mem_used = mem_total = hbm_used = hbm_total = None
            if len(frs) >= 2:
                mem_used, mem_total = frs[0]
                hbm_used, hbm_total = frs[1]
            elif len(frs) == 1:
                hbm_used, hbm_total = frs[0]
            nums = _nums_without_frac(c4)
            aicore = nums[0] if nums else None
            card = last_main['npu'] if last_main else grp
            chips.append({
                'npu': card, 'grp': grp, 'chip': phy, 'bus': bus,
                'name': last_main['name'] if last_main else '',
                'health': last_main['health'] if last_main else '',
                'power': last_main['power'] if last_main else None,
                'temp': last_main['temp'] if last_main else None,
                'aicore': aicore,
                'mem_used': mem_used, 'mem_total': mem_total,
                'hbm_used': hbm_used, 'hbm_total': hbm_total,
                '_busy': False,
            })
            continue

        m = RE_ROW_MAIN.match(line)
        if m and not m.group(2).isdigit():
            c4 = m.group(4) or ''
            nums = _nums_without_frac(c4)
            power = temp = None
            if len(nums) >= 2:
                power, temp = nums[0], nums[1]
            elif len(nums) == 1:
                power = nums[0]
            last_main = {
                'npu': int(m.group(1)),
                'name': m.group(2),
                'health': (m.group(3) or '').strip(),
                'power': power, 'temp': temp,
            }
    return chips


def friendly_ssh_error(msg, rc):
    m = (msg or '').strip()
    first = m.splitlines()[0][:140] if m else ('ssh exit %s' % rc)
    if 'Permission denied' in m:
        return '免密登录失败\n' + first
    if 'Connection refused' in m:
        return '连接被拒绝\n' + first
    if 'timed out' in m or 'unreachable' in m or 'No route' in m:
        return '连接超时\n' + first
    if 'No such host' in m or 'Name or service not known' in m:
        return '主机名无法解析\n' + first
    if 'command not found' in m:
        return '缺少 npu-smi 命令\n' + first
    return first

# ---------------------------------------------------------------------------
# SSH 拉取 + 解析 => snapshot (在采集线程中调用, 不碰 UI)
# ---------------------------------------------------------------------------
def fetch_one(row, g):
    ts0 = time.time()
    host = str(row.get('host', '')).strip()
    user = str(row.get('user', '')).strip()
    try:
        port = int(row.get('port') or 22)
    except (TypeError, ValueError):
        port = 22
    snap = {'ok': False, 'error': '', 'chips': [], 'hostname': '',
            'ts': ts0, 'elapsed': 0.0}
    if not host:
        snap['error'] = '未配置服务器地址'
        return snap

    thr_core = ov(row, g, 'idle_core')
    thr_hbm  = ov(row, g, 'idle_hbm_gb') * 1024.0    # MB
    target = ('%s@%s' % (user, host)) if user else host
    cmd = 'hostname; echo __NPU_END__; npu-smi info 2>&1'
    ct = max(1, int(g.get('connect_timeout', 5)))
    et = max(3, int(g.get('exec_timeout', 25)))
    args = [SSH_BIN, '-p', str(port),
            '-o', 'BatchMode=yes',
            '-o', 'PreferredAuthentications=publickey',
            '-o', 'ConnectTimeout=%d' % ct,
            '-o', 'StrictHostKeyChecking=accept-new',
            target, cmd]
    timeout = ct + et + 4

    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding='utf-8', errors='replace',
                              timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        snap['elapsed'] = time.time() - ts0
        snap['error'] = '超时 (>%ds 无响应)' % timeout
        return snap
    except FileNotFoundError:
        snap['elapsed'] = time.time() - ts0
        snap['error'] = '本机未找到 ssh 客户端'
        return snap
    except Exception as e:
        snap['elapsed'] = time.time() - ts0
        snap['error'] = 'ssh 启动失败: %s' % e
        return snap

    snap['elapsed'] = time.time() - ts0
    out = proc.stdout or ''
    err = (proc.stderr or '').strip()
    head, mark, body = out.partition('__NPU_END__')
    if not mark:
        body = out          # 兜底: 没有标记时仍尝试解析整段输出

    hname = ''
    for ln in head.splitlines():
        s = ln.strip()
        if s and not s.startswith(('+', '|', '=')) and 'Authorized' not in s and '__NPU' not in s:
            hname = s.split()[0]
            break
    snap['hostname'] = hname or host

    chips = parse_npu_smi(body)
    if not chips:
        if proc.returncode != 0:
            snap['error'] = friendly_ssh_error(err or out, proc.returncode)
        else:
            tail = [l for l in out.splitlines() if l.strip()]
            snap['error'] = '未解析到 NPU 数据'
            if tail:
                snap['error'] += '\n' + tail[-1][:120]
        return snap

    for c in chips:
        core_v = c.get('aicore')
        hbm_v  = c.get('hbm_used')
        if core_v is None and hbm_v is None:
            c['_busy'] = True      # 信号缺失时保守处理: 不判定为空闲
        else:
            # 阈值语义: 任一卡 Core > T% 或 HBM已用 > T GB => 占用; T=0 表示只要非 0 即占用
            c['_busy'] = bool((core_v is not None and core_v > thr_core) or
                              (hbm_v is not None and hbm_v > thr_hbm))
    cores  = [c['aicore'] for c in chips if c.get('aicore') is not None]
    temps  = [c['temp'] for c in chips if c.get('temp') is not None]
    powers = [c['power'] for c in chips if c.get('power') is not None]
    snap.update(
        ok=True, chips=chips,
        busy=any(c['_busy'] for c in chips),
        core_avg=(sum(cores) / len(cores)) if cores else None,
        temp_max=max(temps) if temps else None,
        power_sum=sum(powers) if powers else None,
        hbm_used=sum(c.get('hbm_used') or 0 for c in chips),
        hbm_total=sum(c.get('hbm_total') or 0 for c in chips),
        warn=len([c for c in chips if str(c.get('health', '')).upper() not in ('OK', '')]),
    )
    return snap

# ---------------------------------------------------------------------------
# 演示模式 (合成数据, 展示 UI / 弹窗效果)
# ---------------------------------------------------------------------------
DEMO_SERVERS = [
    {'id': 'd1', 'label': '演示-176 持续占用', 'host': '10.0.0.176', 'user': 'root', 'port': 22, 'enabled': True},
    {'id': 'd2', 'label': '演示-177 忙 / 闲交替', 'host': '10.0.0.177', 'user': 'root', 'port': 22, 'enabled': True},
    {'id': 'd3', 'label': '演示-178 持续空闲', 'host': '10.0.0.178', 'user': 'root', 'port': 22, 'enabled': True},
    {'id': 'd4', 'label': '演示-179 时好时坏', 'host': '10.0.0.179', 'user': 'root', 'port': 22, 'enabled': True},
]
_DEMO_ST = {}


def demo_fetch(row, g):
    ts0 = time.time()
    host = row['host']
    st = _DEMO_ST.setdefault(host, {'n': 0})
    st['n'] += 1
    mode = host[-3:]
    if mode == '176':
        cores = [68, 61, 74, 0, 52, 0, 0, 45]
        alloc = [60000, 58000, 61000, 3000, 55000, 3200, 3000, 52000]
    elif mode == '177':
        work = (st['n'] % 30) < 12
        if work:
            cores = [66, 59, 0, 0]
            alloc = [58000, 55000, 3200, 3000]
        else:
            cores = [0, 0, 0, 0]
            alloc = [3050, 2870, 3120, 2880]
    else:
        cores = [0, 0, 0, 0]
        alloc = [3050, 2870, 3120, 2880]

    thr_c = ov(row, g, 'idle_core')
    thr_h = ov(row, g, 'idle_hbm_gb') * 1024.0
    chips = []
    for i, (a, h) in enumerate(zip(cores, alloc)):
        c = {'npu': i // 2, 'grp': i % 2, 'chip': i, 'bus': '0000:8%d:00.0' % i,
             'name': 'Ascend910', 'health': 'OK',
             'power': (165.0 + random.random() * 30) if a else None,
             'temp': 40 + random.randrange(9), 'aicore': float(a),
             'mem_used': 0, 'mem_total': 0,
             'hbm_used': h, 'hbm_total': 65536, '_busy': False}
        c['_busy'] = (c['aicore'] > thr_c) or (c['hbm_used'] > thr_h)
        chips.append(c)
    snap = {'ok': True, 'error': '', 'hostname': 'demo-' + host.split('.')[-1], 'ts': ts0,
            'elapsed': 0.4 + random.random(), 'chips': chips,
            'busy': any(c['_busy'] for c in chips),
            'core_avg': sum(c['aicore'] for c in chips) / len(chips),
            'temp_max': max(c['temp'] for c in chips), 'power_sum': 1240.0,
            'hbm_used': sum(c['hbm_used'] for c in chips),
            'hbm_total': 65536 * len(chips), 'warn': 0}
    if mode == '179' and st['n'] % 10 == 0:
        snap.update(ok=False, error='演示故障: Connection timed out', chips=[])
    return snap

# ---------------------------------------------------------------------------
# 桌面弹窗 (Toast)
# ---------------------------------------------------------------------------
class Toast(tk.Toplevel):
    """Win11 风格桌面弹窗: 左侧事件色条 + 中性标题, 内容自适应, 统一排版不重叠"""

    def __init__(self, app, kind, color, title, lines, sid=None):
        self.app = app
        self.kind = kind
        self.sids = list(sid) if isinstance(sid, (list, tuple)) else ([sid] if sid else [])
        s = max(1.0, getattr(app, 'scale', 1.0))
        tk.Toplevel.__init__(self, app.root)
        self.configure(bg=CARD, highlightthickness=1, highlightbackground=LINE)
        self.overrideredirect(True)
        self.attributes('-topmost', True)

        strip = tk.Frame(self, bg=color, width=int(5 * s))   # Win11 左侧事件色条
        strip.pack(side='left', fill='y')
        wrap = tk.Frame(self, bg=CARD)
        wrap.pack(side='left', fill='both', expand=True)

        # 标题行
        hd = tk.Frame(wrap, bg=CARD)
        hd.pack(fill='x', padx=int(12 * s), pady=(int(10 * s), int(2 * s)))
        tk.Label(hd, text=title, font=F_HDB, bg=CARD, fg=TXT).pack(side='left')
        tk.Button(hd, text='✕', font=F_XS, bd=0, bg=CARD, fg=DIM, cursor='hand2',
                  activebackground=CARD, activeforeground=TXT,
                  command=self.close).pack(side='right')

        # 正文 (最多 3 行, 超长自动折行)
        body = tk.Frame(wrap, bg=CARD)
        body.pack(fill='x', padx=int(14 * s), pady=(int(2 * s), int(6 * s)))
        for i, ln in enumerate([l for l in lines if l][:4]):
            tk.Label(body, text=ln, font=F_SM, bg=CARD,
                     fg=TXT if i == 0 else DIM, anchor='w', justify='left',
                     wraplength=int(240 * s)).pack(fill='x', pady=int(2 * s))

        # 按钮行: pack 左右分布, 天然对齐 (小号按钮)
        bar = tk.Frame(wrap, bg=CARD)
        bar.pack(fill='x', padx=int(12 * s), pady=(0, int(8 * s)))
        if self.sids:
            tk.Button(bar, text='静默 30 分钟', font=F_XS, bd=0, bg=CARD2, fg=DIM,
                      activebackground=CARD2, activeforeground=TXT, cursor='hand2',
                      padx=int(8 * s), pady=int(2 * s),
                      command=self.mute).pack(side='left')
        tk.Button(bar, text='知道了', font=F_XS, bd=0, bg=CARD2, fg=TXT,
                  activebackground=CARD2, activeforeground=TXT, cursor='hand2',
                  padx=int(10 * s), pady=int(2 * s),
                  command=self.close).pack(side='right')

        # 固定尺寸 (适中), 高度随内容; 交给 App 贴桌面右下角统一排版
        self.update_idletasks()
        w = int(280 * s)
        h = int(self.winfo_reqheight())
        self.size = (w, h)
        self.geometry('%dx%d' % (w, h))
        while len(app.toasts) >= 4:              # 最多同时 4 条, 移除最旧
            app.toasts[0].close()
        app.toasts.append(self)
        app._relayout_toasts()
        self.update_idletasks()
        round_window(self, int(10 * s))          # Win11 圆角
        self.after(90000, self.close)
        if app.cfg.get('sound', True) and kind != 'muted':
            try:
                import winsound
                winsound.MessageBeep(
                    winsound.MB_ICONASTERISK if kind == 'idle' else winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass

    def mute(self):
        for sid in self.sids:
            self.app.muted_until[sid] = time.time() + 1800
        if self.sids:
            self.app.logline('静默', '%d 台服务器 30 分钟内不再弹窗' % len(self.sids), 'info')
        self.close()

    def place_at(self, x, y):
        """仅调整位置 (尺寸由内容自适应)"""
        try:
            self.geometry('+%d+%d' % (int(x), int(y)))
        except Exception:
            pass

    def close(self):
        try:
            self.app.toasts.remove(self)
            self.app._relayout_toasts()          # 关闭后其余弹窗回落补位
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# 桌面悬浮小窗 (置顶): 每台服务器一行, 小方块颜色与主界面一致 (状态色)
#   绿=空闲 / 橙=占用 / 黄=健康告警 / 灰=无数据; 可拖动, 悬停气泡, 点击看单卡详情
# ---------------------------------------------------------------------------
class MiniWidget:
    def __init__(self, app):
        self.app = app
        self.s = s = max(1.0, getattr(app, 'scale', 1.0))
        self.tw = max(10, int(15 * s))
        self.th = max(8, int(11 * s))
        self.gap = max(2, int(2 * s))
        self.lw = int(96 * s)
        self.pad = int(6 * s)
        self._sig = None
        self._ver = -1
        self._moved = False
        self._tip = None
        self._menu = None
        self._tip_text = {}

        cfgm = (app.cfg.get('ui') or {}).get('mini') or {}
        self.top = tk.Toplevel(app.root)
        self.top.overrideredirect(True)
        self.top.attributes('-topmost', True)
        self.top.configure(bg=CARD, highlightthickness=1, highlightbackground=LINE)
        self.cv = tk.Canvas(self.top, bg=CARD, highlightthickness=0)
        self.cv.pack(fill='both', expand=True)
        pos = None
        if cfgm.get('geometry'):
            try:
                nums = re.findall(r'[+-]?\d+', str(cfgm['geometry']))
                if len(nums) >= 2:
                    pos = (int(nums[-2]), int(nums[-1]))
            except Exception:
                pos = None
        vl, vt, vr, vb = virtual_screen()
        if vr <= vl:                       # 非 Windows / 获取失败兜底
            vl, vt = 0, 0
            vr, vb = self.top.winfo_screenwidth(), self.top.winfo_screenheight()
        if pos is not None and not (       # 保存的位置已不可见 → 回默认位置
                vl - 20 <= pos[0] <= vr - int(120 * s) and
                vt - 20 <= pos[1] <= vb - int(60 * s)):
            pos = None
        if pos is None:
            wa = app._screen_anchor()
            pos = (wa[2] - int(500 * s), wa[1] + int(60 * s))
        # 钳制在可见屏幕范围内
        pos = (min(max(pos[0], vl), max(vl, vr - int(120 * s))),
               min(max(pos[1], vt), max(vt, vb - int(60 * s))))
        self._pos = pos
        self.top.geometry('+%d+%d' % self._pos)
        # Windows 下窗口映射前设置 topmost 可能不生效: 映射后/定时再补设
        self.top.bind('<Map>', lambda e: self._assert_top(), add='+')
        self.top.after(200, self._assert_top)

    def _assert_top(self):
        """确保悬浮窗置顶可见"""
        try:
            self.top.attributes('-topmost', True)
            self.top.lift()
        except Exception:
            pass
        self.cv.bind('<Button-1>', self._press)
        self.cv.bind('<B1-Motion>', self._motion)
        self.cv.bind('<ButtonRelease-1>', self._release)
        self.cv.bind('<Button-3>', self._rclose)     # 右键任意位置关闭
        self.refresh()

    # ---- 拖动 / 关闭 / 点击 ----
    def _press(self, ev):
        self._moved = False
        self._dx, self._dy = ev.x, ev.y

    def _motion(self, ev):
        try:
            if abs(ev.x - self._dx) + abs(ev.y - self._dy) > 4:
                self._moved = True
            self._pos = (ev.x_root - self._dx, ev.y_root - self._dy)
            self.top.geometry('+%d+%d' % self._pos)
        except Exception:
            pass

    def _release(self, ev):
        if self._moved:
            return
        try:
            item = self.cv.find_closest(ev.x, ev.y)[0]
            tags = self.cv.gettags(item)
        except Exception:
            return
        if 'x' in tags:
            self.app.close_mini()
            return
        for t in tags:
            if t.startswith('t:'):
                parts = t.split(':')
                if len(parts) == 3:
                    self.app.open_chip_from_mini(parts[1], int(parts[2]))
                return

    def _rclose(self, ev):
        """右键: 弹出与整体 UI 统一的深色小菜单, 点击「关闭悬浮窗」后才关闭"""
        self._menu_close()
        try:
            s = self.s
            m = tk.Toplevel(self.top)
            m.overrideredirect(True)
            m.attributes('-topmost', True)
            m.configure(bg=CARD, highlightthickness=1, highlightbackground=LINE)
            b = tk.Button(m, text='关闭悬浮窗', font=F_SM, bd=0, relief='flat',
                          bg=CARD, fg=TXT, activebackground=CARD2,
                          activeforeground=TXT, anchor='w', justify='left',
                          padx=int(14 * s), pady=int(6 * s), cursor='hand2',
                          command=self._menu_close_mini)
            b.pack(fill='x')
            hover_btn(b, CARD, CARD2)
            m.update_idletasks()
            w, h = m.winfo_reqwidth(), m.winfo_reqheight()
            sw, sh = self.top.winfo_screenwidth(), self.top.winfo_screenheight()
            x, y = ev.x_root + 2, ev.y_root + 2
            if x + w > sw - 4:
                x = ev.x_root - w - 2
            if y + h > sh - 4:
                y = ev.y_root - h - 2
            m.geometry('+%d+%d' % (max(0, x), max(0, y)))
            round_window(m, int(8 * s))
            m.grab_set()                    # 点击菜单外任意位置 → 收起菜单
            m.bind('<Button-1>', self._menu_close)
            m.bind('<Escape>', self._menu_close)
            self._menu = m
        except Exception:
            self._menu = None

    def _menu_close(self, _ev=None):
        m = getattr(self, '_menu', None)
        if m is not None:
            self._menu = None
            try:
                m.grab_release()
            except Exception:
                pass
            try:
                m.destroy()
            except Exception:
                pass

    def _menu_close_mini(self):
        self._menu_close()
        self.app.close_mini()

    # ---- 悬停气泡 ----
    def _tip_show(self, text, ev):
        self._tip_hide()
        try:
            self._tip = tp = tk.Toplevel(self.top)
            tp.overrideredirect(True)
            tp.attributes('-topmost', True)
            tk.Label(tp, text=text, font=F_XS, bg=T_TRACK, fg=TXT,
                     justify='left', padx=6, pady=3).pack()
            tp.geometry('+%d+%d' % (ev.x_root + 12, ev.y_root + 14))
        except Exception:
            self._tip = None

    def _tip_move(self, ev):
        if self._tip:
            try:
                self._tip.geometry('+%d+%d' % (ev.x_root + 12, ev.y_root + 14))
            except Exception:
                pass

    def _tip_hide(self, _ev=None):
        if self._tip:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None

    @staticmethod
    def _chip_color(chip):
        """与主界面每卡方块完全一致的状态色 (绿=空闲/橙=占用/黄=健康告警)"""
        health = str(chip.get('health', '')).upper()
        if health and health != 'OK':
            return T_WARN_BORDER
        if chip.get('_busy'):
            return WARNC
        return OKC

    # ---- 绘制 ----
    def refresh(self):
        app = self.app
        cv, s = self.cv, self.s
        servers = app.cfg['servers']
        sig = tuple((r['id'], len((app.last.get(r['id']) or {}).get('chips', [])),
                     (app.last.get(r['id']) or {}).get('ok')) for r in servers)
        if not (sig != self._sig or self._ver != app.data_ver):
            return
        self._sig, self._ver = sig, app.data_ver
        cv.delete('all')
        self._tip_hide()
        tw, th, gap, lw, pad = self.tw, self.th, self.gap, self.lw, self.pad
        cols_max = max(1, int((int(560 * s) - lw) / (tw + gap)))
        y = pad
        w_need = lw + pad * 2
        st_map = {'idle': ('闲', OKC), 'busy': ('忙', WARNC), 'fail': ('断', ERRC),
                  'off': ('停', DIM), 'checking': ('…', DIM)}
        for row in servers:
            sid = row['id']
            enabled = row.get('enabled', True)
            snap = app.last.get(sid)
            chips = snap.get('chips') if (snap and snap.get('ok')) else []
            rt = app._rt(sid)
            st_txt, scol = st_map.get(rt.get('state'), ('…', DIM))
            if not enabled:
                st_txt, scol = '停', DIM
            label = (row.get('label') or row['host'])
            n = max(1, len(chips))
            cols = max(1, min(cols_max, n))
            rows_n = int(math.ceil(n / float(cols)))
            cv.create_text(pad, y + int(th / 2.0), anchor='w', font=F_XS,
                           text='%s %s' % (st_txt, label[:11]), fill=scol)
            if chips:
                for i, c in enumerate(chips):
                    cx = lw + (i % cols) * (tw + gap)
                    cy = y + (i // cols) * (th + gap)
                    tname = 't:%s:%d' % (sid, i)
                    cv.create_rectangle(cx, cy, cx + tw, cy + th,
                                        fill=self._chip_color(c), outline='',
                                        tags=(tname,))
                    tip = '%s  P%s\nCore %s · HBM %s/%sG · %s°C' % (
                        label, c.get('chip', i),
                        ('%.1f%%' % c['aicore']) if c.get('aicore') is not None else '—',
                        fmt_gb(c.get('hbm_used')), fmt_gb(c.get('hbm_total'), 0),
                        c.get('temp') if c.get('temp') is not None else '--')
                    cv.tag_bind(tname, '<Enter>',
                                lambda e, t=tip: self._tip_show(t, e))
                    cv.tag_bind(tname, '<Motion>', self._tip_move)
                    cv.tag_bind(tname, '<Leave>', self._tip_hide)
            else:
                cv.create_text(lw, y + int(th / 2.0), anchor='w', font=F_XS,
                               text='无数据' if enabled else '未启用', fill=DIM)
            y += rows_n * (th + gap) + gap
            w_need = max(w_need, lw + min(cols, len(chips) or 1) * (tw + gap) + pad * 2)
        cv.create_text(w_need - pad, int(3 * s), anchor='ne', text='✕',
                       font=F_XS, fill=DIM, tags=('x',))
        cv.tag_bind('x', '<Enter>', lambda e: cv.configure(cursor='hand2'))
        cv.tag_bind('x', '<Leave>', lambda e: cv.configure(cursor=''))
        w = max(int(180 * s), w_need)
        h = y + pad
        self.top.geometry('%dx%d+%d+%d' % (w, h, self._pos[0], self._pos[1]))
        self.top.update_idletasks()
        round_window(self.top, int(10 * s))      # Win11 圆角
        self._assert_top()                       # 每次刷新保持置顶


# ---------------------------------------------------------------------------
# 单台服务器行 (标题行 + 每卡小方块 canvas + 迷你趋势图)
# ---------------------------------------------------------------------------
class Row:
    def __init__(self, app, parent, cfgrow):
        self.app = app
        self.sid = cfgrow['id']
        self.cfgrow = cfgrow
        s = max(1.0, getattr(app, 'scale', 1.0))
        self.f = tk.Frame(parent, bg=CARD, bd=0, highlightthickness=1,
                          highlightbackground=LINE)
        self.f.pack(fill='x', padx=int(10 * s), pady=int(6 * s))

        head1 = tk.Frame(self.f, bg=CARD)
        head1.pack(fill='x', padx=int(10 * s), pady=(int(8 * s), 0))

        self.dot = tk.Label(head1, text='●', font=F_HD, bg=CARD, fg=DIM)
        self.dot.pack(side='left')

        self.title = tk.Label(head1, text=self._title_text(), font=F_HDB,
                              bg=CARD, fg=TXT, cursor='hand2')
        self.title.pack(side='left', padx=(int(6 * s), int(6 * s)))
        self.title.bind('<Double-Button-1>', lambda e: app.edit_row(self.sid))

        self.state = tk.Label(head1, text='等待数据…', font=F_SM, bg=CARD, fg=DIM)
        self.state.pack(side='left', padx=(int(14 * s), 0))

        rgt = tk.Frame(head1, bg=CARD)
        rgt.pack(side='right')
        for txt, cb in (('测试', lambda: app.test_row(self.sid)),
                        ('编辑', lambda: app.edit_row(self.sid)),
                        ('删除', lambda: app.del_row(self.sid))):
            b = tk.Button(rgt, text=txt, font=F_XS, bd=0, relief='flat',
                          bg=CARD, fg=DIM, activebackground=CARD2,
                          activeforeground=TXT, cursor='hand2',
                          padx=int(8 * s), pady=1, command=cb)
            b.pack(side='left', padx=int(3 * s))
            hover_btn(b, CARD, CARD2, DIM, TXT)

        head2 = tk.Frame(self.f, bg=CARD)
        head2.pack(fill='x', padx=int(10 * s), pady=(int(4 * s), 0))

        self.spark = tk.Canvas(head2, width=SPARK_W, height=SPARK_H,
                               bg=CARD, highlightthickness=0)
        self.spark.pack(side='right')

        self.tstamp = tk.Label(head2, text='', font=F_NUM, bg=CARD, fg=DIM, anchor='e')
        self.tstamp.pack(side='right', padx=(0, int(10 * s)))

        self.info = tk.Label(head2, text='', font=F_NUM, bg=CARD, fg=DIM, anchor='w')
        self.info.pack(side='left', fill='x', expand=True)

        self.tiles = tk.Canvas(self.f, bg=CARD, height=CHIP_DY, highlightthickness=0)
        self.tiles.pack(fill='x', padx=int(10 * s), pady=(int(4 * s), int(8 * s)))
        self.tiles.bind('<Button-1>', self._tile_click)
        self.tiles.bind('<Configure>', self._maybe_rerender)

        self._inited = False

    def _title_text(self):
        row = self.cfgrow
        label = row.get('label') or row['host']
        user = row.get('user') or ''
        return ('%s  ·  %s@%s' % (label, user, row['host'])) if user else label

    def _tile_click(self, ev):
        try:
            tags = self.tiles.gettags('current')
        except Exception:
            return
        for t in tags:
            if t.startswith('idx:'):
                i = int(t.split(':', 1)[1])
                snap = self.app.last.get(self.sid)
                if snap and snap.get('ok') and i < len(snap.get('chips', [])):
                    self.app.chip_detail(self.sid, snap['chips'][i])
                return

    def _maybe_rerender(self, ev):
        if ev.width > 6:
            self.app.dirty.add(self.sid)

# ---------------------------------------------------------------------------
# 迷你趋势图
# ---------------------------------------------------------------------------
def draw_sparkline(cv, vals, col):
    cv.delete('all')
    w, h = SPARK_W, SPARK_H
    cv.create_line(0, h - 2, w, h - 2, fill=LINE)
    nums = [v for v in vals if v is not None]
    if not nums:
        cv.create_text(w / 2, h / 2 - 2, text='— 趋势', font=F_XS, fill=DIM)
        return
    mx = max(max(nums), 5.0)
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        if v is None:
            continue
        x = i * (float(w - 4) / max(n - 1, 1)) + 2
        y = (h - 5) * (1.0 - min(v, mx) / mx) + 4
        pts.append((x, y))
    if len(pts) >= 2:
        cv.create_line(*[c for p in pts for c in p], fill=col, width=1)
    lx, ly = pts[-1]
    cv.create_oval(lx - 2, ly - 2, lx + 2, ly + 2, fill=col, outline='')
    cv.create_text(2, 0, anchor='nw', text='Core%', font=F_XS, fill=DIM)

# ---------------------------------------------------------------------------
# 主应用
# ---------------------------------------------------------------------------
class MonitorApp:
    def __init__(self, root, demo=False):
        self.root = root
        self.demo = demo
        self.cfg = load_cfg()
        if self.demo:
            self.cfg['servers'] = json.loads(json.dumps(DEMO_SERVERS))
            self.cfg['defaults']['idle_cycles'] = 2
            self.cfg['interval'] = 3

        # DPI: 字体随系统缩放自动变大, 画布像素常量需同步缩放
        try:
            self.scale = max(1.0, float(self.root.winfo_fpixels('1i')) / 96.0)
        except Exception:
            self.scale = 1.0
        apply_ui_scale(self.scale)

        self.q = queue.Queue()
        self.stop = threading.Event()
        self.paused = False
        self.muted_until = {}
        self.toasts = []
        self.rows = {}
        self.last = {}
        self.rt = {}
        self.dirty = set()
        self.workers = []
        self.kick_events = {}
        self.mini = None          # 悬浮小窗 (MiniWidget)
        self.data_ver = 0         # 数据版本号 (悬浮窗增量刷新用)
        self._pending_toasts = [] # 待合并弹窗事件 (同类合并为一条)
        self._id_seed = int(time.time() * 1000) % 100000

        self._build()
        if not self.demo and not os.path.isfile(CFG_PATH):
            save_cfg(self.cfg)      # 首次运行: 生成可手工编辑的配置文件
        self.rebuild_rows()
        self._restart_workers()
        if (self.cfg.get('ui') or {}).get('mini', {}).get('visible'):
            self.root.after(600, self.open_mini)   # 恢复上次会话的悬浮窗
        self.root.after(400, self._tick)
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.root.bind('<F5>', lambda e: self.refresh_now())

    # ---------------- 布局 ----------------
    def _build(self):
        title = APP_TITLE + '  ' + VER
        if self.demo:
            title += ' — 演示模式'
        self.root.title(title)
        self.root.configure(bg=BG)
        geo = (self.cfg.get('ui') or {}).get('geometry')
        if geo:
            try:
                self.root.geometry(geo)
            except Exception:
                pass
        else:
            self.root.geometry('%dx%d' % (int(1020 * self.scale), int(700 * self.scale)))
        self.root.minsize(int(860 * self.scale), int(540 * self.scale))

        bar = tk.Frame(self.root, bg=BG)
        bar.pack(side='top', fill='x')
        tk.Label(bar, text='NPU 占用监控', font=F_T, bg=BG, fg=TXT).pack(
            side='left', padx=14, pady=9)
        self._bar_btn(bar, '＋ 添加服务器', self.add_row)
        self._bar_btn(bar, '⇩ 导入SSH配置', self.import_vscode_ssh)
        self._bar_btn(bar, '设置', self.open_settings)
        self.btn_pause = self._bar_btn(bar, '⏸ 暂停', self.toggle_pause)
        self._bar_btn(bar, '⟳ 立即刷新 (F5)', self.refresh_now)
        self.btn_mini = self._bar_btn(bar, '▦ 悬浮窗', self.toggle_mini)

        sb = tk.Frame(self.root, bg=BG)
        sb.pack(side='bottom', fill='x')
        logwrap = tk.Frame(self.root, bg=BG, height=110)
        logwrap.pack(side='bottom', fill='x')
        self.sb = tk.Label(sb, text='', font=F_SM, bg=BG, fg=DIM, anchor='w')
        self.sb.pack(side='left', padx=12, pady=4)
        self.logwin = tk.Text(logwrap, bg=BG, fg=DIM, font=F_NUM, height=4, bd=0,
                              wrap='none', state='disabled', padx=10, pady=4)
        self.logwin.pack(fill='both', expand=True)
        for tag, col in (('idle', OKC), ('busy', WARNC), ('fail', ERRC), ('info', DIM)):
            self.logwin.tag_configure(tag, foreground=col)

        mid = tk.Frame(self.root, bg=BG)
        mid.pack(side='top', fill='both', expand=True)
        self.canvas = tk.Canvas(mid, bg=BG, highlightthickness=0, bd=0)
        try:                                   # Win11 细滚动条
            st = ttk.Style(self.root)
            st.theme_use('clam')
            st.configure('Vertical.TScrollbar', background=CARD2, troughcolor=BG,
                         bordercolor=BG, arrowcolor=DIM, relief='flat',
                         arrowsize=11)
            st.map('Vertical.TScrollbar',
                   background=[('active', LINE), ('pressed', ACC)],
                   arrowcolor=[('active', TXT)])
        except Exception:
            pass
        vsb = ttk.Scrollbar(mid, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.canvas.create_window((0, 0), window=self.inner, anchor='nw', tags='INNER')
        self.canvas.bind('<Configure>',
                         lambda e: self.canvas.itemconfigure('INNER', width=e.width))
        self.inner.bind('<Configure>', lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox('all')))
        self.canvas.bind_all('<MouseWheel>',
                             lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, 'units'))
        win11_titlebar(self.root)
        self.root.bind('<Map>', lambda e: win11_titlebar(self.root), add='+')
        self.root.after(300, lambda: win11_titlebar(self.root))

    def _bar_btn(self, parent, text, cmd):
        b = tk.Button(parent, text=text, font=F_SM, bd=0, relief='flat',
                      bg=CARD, fg=TXT, activebackground=CARD2,
                      activeforeground='#ffffff', padx=12, pady=5,
                      cursor='hand2', command=cmd)
        b.pack(side='left', padx=(0, 6), pady=6)
        hover_btn(b, CARD, CARD2)
        return b

    # ---------------- 行 / 运行态 ----------------
    def _rt(self, sid):
        return self.rt.setdefault(sid, {'state': 'checking', 'idle_since': None,
                                        'idle_streak': 0, 'alerted': False,
                                        'last_alert': 0, 'hist': deque(maxlen=180)})

    def _row_cfg(self, sid):
        for r in self.cfg['servers']:
            if r['id'] == sid:
                return r
        return None

    def rebuild_rows(self):
        for r in self.rows.values():
            try:
                r.f.destroy()
            except Exception:
                pass
        self.rows.clear()
        self.rt.clear()
        self.last.clear()
        self.dirty.clear()
        if not self.cfg['servers']:
            ph = tk.Frame(self.inner, bg=BG)      # 空状态引导页
            ph.pack(fill='x', padx=10, pady=(int(40 * self.scale),))
            tk.Label(ph, text='尚未添加服务器', font=('Microsoft YaHei UI', 15, 'bold'),
                     bg=BG, fg=TXT).pack()
            tk.Label(ph, text='点击上方「＋ 添加服务器」手工添加，或「⇩ 导入SSH配置」\n自动读取 VSCode Remote-SSH / ~/.ssh/config 的主机列表',
                     font=F_SM, bg=BG, fg=DIM, justify='center').pack(
                pady=(int(10 * self.scale), int(40 * self.scale)))
            return
        for row in self.cfg['servers']:
            self.rows[row['id']] = Row(self, self.inner, row)
            self.rt[row['id']] = self._rt(row['id'])
            self.last[row['id']] = None
            self.dirty.add(row['id'])

    # ---------------- 后台轮询线程 ----------------
    def _restart_workers(self):
        for _t, ev in self.workers:
            ev.set()
        self.workers = []
        self.kick_events = {}
        for row in self.cfg['servers']:
            ev = threading.Event()
            self.kick_events[row['id']] = threading.Event()
            t = threading.Thread(target=self._worker, args=(row, ev), daemon=True,
                                 name='poll-%s' % row['id'])
            t.start()
            self.workers.append((t, ev))

    def _worker(self, row, ev):
        sid = row['id']
        while not (self.stop.is_set() or ev.is_set()):
            if self.paused or not row.get('enabled', True):
                if self.stop.wait(1.0) or ev.is_set():
                    return
                continue
            t0 = time.time()
            try:
                if self.demo:
                    snap = demo_fetch(row, self.cfg)
                else:
                    snap = fetch_one(row, self.cfg)
            except Exception as e:
                snap = {'ok': False, 'error': '采集异常: %s' % e, 'chips': [],
                        'hostname': '', 'ts': time.time(), 'elapsed': 0.0}
            self.q.put(('snap', sid, snap))
            left = max(1.0, float(self.cfg.get('interval', 15)) - (time.time() - t0))
            end = time.time() + left
            kev = self.kick_events.get(sid)
            while time.time() < end:
                if self.stop.wait(0.4) or ev.is_set():
                    return
                if kev is not None and kev.is_set():
                    kev.clear()
                    break

    # ---------------- 定时器 ----------------
    def _tick(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item and item[0] == 'snap':
                    self._apply(item[1], item[2])
                elif item and item[0] == 'test':
                    self._show_test_result(item[1], item[2])
        except queue.Empty:
            pass
        if self._pending_toasts and \
                time.time() - min(p['ts'] for p in self._pending_toasts) >= 1.2:
            self._flush_toasts()      # 1.2s 窗口内的同类事件合并为一条弹窗

        for sid in list(self.dirty):
            self.dirty.discard(sid)
            self.render_row(sid, force=True)
        for sid in list(self.rows):
            self.render_row(sid)      # 轻量刷新: 空闲计时滚动
        if self.mini is not None:
            try:
                self.mini.refresh()
            except Exception:
                pass
        self._statusbar()
        self.root.after(500, self._tick)

    def _apply(self, sid, snap):
        row = self._row_cfg(sid)
        if row is None:
            return
        self.data_ver += 1
        rt = self._rt(sid)
        name = row.get('label') or snap.get('hostname') or row['host']
        self.last[sid] = snap
        self.dirty.add(sid)

        if not snap.get('ok'):
            rt['hist'].append((snap['ts'], None))
            if rt.get('state') != 'fail':
                self.logline(name, ('连接失败: %s' % snap.get('error', '')).replace('\n', ' | '), 'fail')
                if self.cfg.get('alert_fail', True) and not self._muted(sid):
                    lines = (snap.get('error') or '未知错误').split('\n')
                    extra = [lines[0]]
                    if len(lines) > 1:
                        extra.append(lines[1][:60])
                    extra.append('目标: %s@%s' % (row.get('user') or '-', row['host']))
                    self._pending_toasts.append({
                        'kind': 'fail', 'ts': time.time(), 'sid': sid, 'name': name,
                        'title': '连接失败 · %s' % name,
                        'summary': fit_line('%s  %s' % (name, lines[0])),
                        'lines': extra})
            rt.update(state='fail', idle_since=None, idle_streak=0, alerted=False)
            return

        rt['hist'].append((snap['ts'], snap.get('core_avg')))
        if rt.get('state') == 'fail':
            self.logline(name, '恢复连接 (%.1fs)' % snap['elapsed'], 'info')

        if snap.get('busy'):
            if rt.get('idle_since'):
                d = snap['ts'] - rt['idle_since']
                if self.cfg.get('alert_recover') and d >= 60 and not self._muted(sid):
                    self._pending_toasts.append({
                        'kind': 'busy', 'ts': time.time(), 'sid': sid, 'name': name,
                        'title': '占用恢复 · %s' % name,
                        'summary': fit_line('%s  空闲 %s 后恢复占用' % (name, fmt_dur(d))),
                        'lines': ['空闲 %s 后恢复正常' % fmt_dur(d),
                                  'Core %s · HBM %s/%s GB' % (
                                      fmt_pct(snap.get('core_avg')),
                                      fmt_gb(snap.get('hbm_used')),
                                      fmt_gb(snap.get('hbm_total')))]})
                self.logline(name, '恢复占用 (此前空闲 %s)' % fmt_dur(d), 'busy')
            rt.update(state='busy', idle_since=None, idle_streak=0, alerted=False)
            return

        # ---- 空闲侧 ----
        if rt.get('idle_since') is None:
            rt['idle_since'] = snap['ts']
        rt['idle_streak'] = int(rt.get('idle_streak', 0)) + 1
        rt['state'] = 'idle'
        need = max(1, int(ov(row, self.cfg, 'idle_cycles')))
        renn = float(self.cfg.get('renotify_min', 30)) * 60.0
        fired = False
        if rt['idle_streak'] == need and not rt.get('alerted'):
            fired = True
            rt['alerted'] = True
            rt['last_alert'] = snap['ts']
            self.logline(name, '确认空闲 (连续 %d 轮 / %s)' % (
                rt['idle_streak'], fmt_dur(snap['ts'] - rt['idle_since'])), 'idle')
        elif rt.get('alerted') and renn > 0 and snap['ts'] - rt.get('last_alert', 0) >= renn:
            fired = True
            rt['last_alert'] = snap['ts']
            self.logline(name, '空闲持续 (%s)' % fmt_dur(snap['ts'] - rt['idle_since']), 'idle')
        if fired and self.cfg.get('alert_idle', True) and not self._muted(sid):
            idle_s = snap['ts'] - rt['idle_since']
            self._pending_toasts.append({
                'kind': 'idle', 'ts': time.time(), 'sid': sid, 'name': name,
                'title': '%s 已空闲 %s' % (name, fmt_dur(idle_s)),
                'summary': fit_line('%s  空闲 %s · HBM %s/%sG' % (
                    name, fmt_dur(idle_s),
                    fmt_gb(snap.get('hbm_used')), fmt_gb(snap.get('hbm_total'), 0))),
                'lines': ['%d 卡 · Core %s · HBM %s/%s GB' % (
                              len(snap.get('chips', [])),
                              fmt_pct(snap.get('core_avg')),
                              fmt_gb(snap.get('hbm_used')),
                              fmt_gb(snap.get('hbm_total'))),
                          '主机: %s' % snap.get('hostname', '-'),
                          '判定: 全部卡 Core≤%s%% 且 HBM≤%sG' % (
                              ov(row, self.cfg, 'idle_core'),
                              ov(row, self.cfg, 'idle_hbm_gb'))]})

    def _muted(self, sid):
        return time.time() < self.muted_until.get(sid, 0)

    # ---------------- 渲染 ----------------
    def render_row(self, sid, force=False):
        rw = self.rows.get(sid)
        row = self._row_cfg(sid)
        if rw is None or row is None:
            return
        rt = self._rt(sid)
        snap = self.last.get(sid)

        enabled = row.get('enabled', True)
        st = rt.get('state', 'checking')
        if not enabled:
            text, col = '未启用', DIM
        elif snap is None or st == 'checking':
            text, col = ('已暂停' if self.paused else '等待数据…'), DIM
        elif not snap.get('ok'):
            text, col = '不可达', ERRC
        elif st == 'idle':
            d = (snap['ts'] - rt['idle_since']) if rt.get('idle_since') else 0
            text, col = '空闲 %s' % fmt_dur(d), OKC
        elif st == 'busy':
            text, col = '占用中', WARNC
            if self.paused:
                text += ' (暂停)'
        else:
            text, col = ('已暂停' if self.paused else '等待数据…'), DIM
        rw.state.configure(text=text, fg=col)
        rw.dot.configure(fg=col)

        if snap and snap.get('ok'):
            warn = '  ⚠%d' % snap['warn'] if snap.get('warn') else ''
            info = '%d 卡%s   Core %s   HBM %s / %s G   %s°C   %sW' % (
                len(snap['chips']), warn,
                fmt_pct(snap.get('core_avg')),
                fmt_gb(snap.get('hbm_used')), fmt_gb(snap.get('hbm_total')),
                ('%d' % snap['temp_max']) if snap.get('temp_max') is not None else '--',
                ('%.0f' % snap['power_sum']) if snap.get('power_sum') else '--')
            rw.info.configure(text=info, fg=WARNC if st == 'busy' else DIM)
            rw.tstamp.configure(text='上次 %s  ·  %.1fs' % (hhmm(snap['ts']), snap['elapsed']),
                                fg=DIM)
        elif snap is not None and not snap.get('ok'):
            rw.info.configure(text=(snap.get('error', '').split('\n')[0] or '失败')[:110], fg=ERRC)
            rw.tstamp.configure(text='', fg=DIM)
        else:
            rw.info.configure(
                text='判占用: 任一卡 Core>%s%% 或 HBM>%sG (0=非0即占用) · 空闲需连续 %s 轮' % (
                    ov(row, self.cfg, 'idle_core'),
                    ov(row, self.cfg, 'idle_hbm_gb'),
                    ov(row, self.cfg, 'idle_cycles')),
                fg=DIM)
            rw.tstamp.configure(text='', fg=DIM)

        if force:
            self._paint_tiles(sid, rw, snap, enabled)
            draw_sparkline(rw.spark,
                           [v for (_t, v) in list(rt['hist'])[-60:]],
                           col)

    def _paint_tiles(self, sid, rw, snap, enabled):
        cv = rw.tiles
        w = int(cv.winfo_width() or 0)
        if w < 40:
            self.dirty.add(sid)     # 宽度未就绪, 下一轮再画
            return
        cv.delete('all')
        if not enabled:
            cv.create_text(w / 2, CHIP_DY / 2, text='已禁用 — 双击标题可编辑',
                           font=F_SM, fill=DIM)
            cv.configure(height=CHIP_DY)
            return
        chips = snap.get('chips') if (snap and snap.get('ok')) else None
        if not chips:
            cv.create_text(w / 2, CHIP_DY / 2, text='无数据', font=F_SM, fill=DIM)
            cv.configure(height=CHIP_DY)
            return
        per_row = max(1, int((w - CHIP_PAD * 2) // CHIP_DX))
        rows_n = max(1, int(math.ceil(len(chips) / float(per_row))))
        height = int(rows_n * CHIP_DY + 2)
        if int(str(cv.cget('height'))) != height:
            cv.configure(height=height)
        for i, c in enumerate(chips):
            col = i % per_row
            rw_ = i // per_row
            x = CHIP_PAD + col * CHIP_DX
            y = 2 + rw_ * CHIP_DY
            health = str(c.get('health', '')).upper()
            if health and health != 'OK':
                border, bgc = T_WARN_BORDER, T_WARN_BG
            elif c.get('_busy'):
                border, bgc = WARNC, T_BUSY_BG
            else:
                border, bgc = OKC, T_IDLE_BG
            tags = ('chip', 'idx:%d' % i)
            cv.create_rectangle(x, y, x + CHIP_W, y + CHIP_H, fill=bgc,
                                outline=border, width=1, tags=tags)
            # 文字/进度条按方块尺寸比例定位, 任意 DPI/字号下不重叠
            ix = int(CHIP_W * 0.09)
            cv.create_text(x + ix, y + int(CHIP_H * 0.09), anchor='nw',
                           text='P%d' % c.get('chip', i),
                           font=F_TILES, fill=DIM, tags=tags)
            temp = c.get('temp')
            if temp is not None:
                tcol = ERRC if temp >= 70 else DIM      # 仅高温标红, 减少色彩噪音
                cv.create_text(x + CHIP_W - ix, y + int(CHIP_H * 0.10), anchor='ne',
                               text='%d°C' % temp, font=F_TILES, fill=tcol, tags=tags)
            core = c.get('aicore')
            core_txt = '—' if core is None else ('%.1f%%' % core)
            cv.create_text(x + CHIP_W / 2.0, y + int(CHIP_H * 0.42), text=core_txt,
                           font=F_TILE, fill=border, tags=tags)
            hu, ht = c.get('hbm_used'), c.get('hbm_total')
            hbm_txt = '—' if hu is None else ('%sG' % fmt_gb(hu))
            cv.create_text(x + CHIP_W / 2.0, y + int(CHIP_H * 0.65), font=F_TILES,
                           text=hbm_txt, fill=DIM, tags=tags)
            # HBM 占用进度条: "显存满载但 Core=0" 的状态一眼可辨
            ratio = 0.0
            if hu is not None and ht:
                ratio = max(0.0, min(1.0, float(hu) / float(ht)))
            bx0, bx1 = x + int(CHIP_W * 0.10), x + CHIP_W - int(CHIP_W * 0.10)
            by0, by1 = y + int(CHIP_H * 0.82), y + int(CHIP_H * 0.92)
            cv.create_rectangle(bx0, by0, bx1, by1, fill=T_TRACK,
                                outline=LINE, tags=tags)
            # 进度条与方块状态同色系: 长度表示占用比例, 不再按比例换色
            if ratio > 0.005:
                bw = max(2, int((bx1 - bx0) * ratio))
                cv.create_rectangle(bx0, by0, bx0 + bw, by1, fill=border,
                                    outline='', tags=tags)

    # ---------------- 状态栏 / 日志 ----------------
    def _statusbar(self):
        n_busy = n_idle = n_fail = n_off = 0
        for sid in self.rows:
            row = self._row_cfg(sid)
            if row and not row.get('enabled', True):
                n_off += 1
                continue
            st = self._rt(sid).get('state')
            if st == 'fail':
                n_fail += 1
            elif st == 'busy':
                n_busy += 1
            elif st == 'idle':
                n_idle += 1
        parts = ['共 %d 台' % len(self.cfg['servers'])]
        if n_busy:
            parts.append('占用 %d' % n_busy)
        if n_idle:
            parts.append('空闲 %d' % n_idle)
        if n_fail:
            parts.append('不可达 %d' % n_fail)
        if n_off:
            parts.append('未启用 %d' % n_off)
        if self.paused:
            parts.append('已暂停')
        self.sb.configure(text='  ·  '.join(parts) +
                          '   |   轮询 %ds  提醒[空闲%s·恢复%s·离线%s] 声音%s  日志: %s' % (
                              self.cfg.get('interval', 15),
                              '开' if self.cfg.get('alert_idle') else '关',
                              '开' if self.cfg.get('alert_recover') else '关',
                              '开' if self.cfg.get('alert_fail') else '关',
                              '开' if self.cfg.get('sound') else '关',
                              os.path.basename(LOG_PATH)))

    def logline(self, srv, msg, kind='info'):
        try:
            self.logwin.configure(state='normal')
            self.logwin.insert('end', '[%s] %s — %s\n' % (
                time.strftime('%H:%M:%S'), srv, msg.replace('\n', ' | ')), kind)
            try:
                nl = int(self.logwin.index('end-1c').split('.')[0])
                if nl > 300:
                    self.logwin.delete('1.0', '%d.0' % (nl - 200))
            except Exception:
                pass
            self.logwin.see('end')
            self.logwin.configure(state='disabled')
        except Exception:
            pass
        log_event(kind.upper(), srv, msg)

    def _screen_anchor(self):
        """弹窗锚点 = 主窗口所在显示器的右下角工作区 (多显示器 / 自动避开任务栏)"""
        if IS_WIN:
            try:
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32

                class MONITORINFO(ctypes.Structure):
                    _fields_ = [('cbSize', wintypes.DWORD),
                                ('rcMonitor', wintypes.RECT),
                                ('rcWork', wintypes.RECT),
                                ('dwFlags', wintypes.DWORD)]

                user32.GetParent.restype = wintypes.HWND
                user32.GetParent.argtypes = [wintypes.HWND]
                user32.MonitorFromWindow.restype = wintypes.HANDLE
                user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
                user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE,
                                                   ctypes.POINTER(MONITORINFO)]

                hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
                mon = user32.MonitorFromWindow(hwnd, 2)      # MONITOR_DEFAULTTONEAREST
                if mon:
                    mi = MONITORINFO()
                    mi.cbSize = ctypes.sizeof(MONITORINFO)
                    if user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                        return (int(mi.rcWork.left), int(mi.rcWork.top),
                                int(mi.rcWork.right), int(mi.rcWork.bottom))
            except Exception:
                pass
        s = max(1.0, getattr(self, 'scale', 1.0))
        return (0, 0,
                int(self.root.winfo_screenwidth()),
                max(0, int(self.root.winfo_screenheight()) - int(60 * s)))

    def _relayout_toasts(self):
        """统一排版: 弹窗置顶贴住主窗口所在屏的【桌面右下角】(工作区, 避开任务栏)"""
        try:
            wa = self._screen_anchor()             # (left, top, right, bottom)
            s = max(1.0, self.scale)
            m = int(6 * s)
            gap = int(8 * s)
            x = wa[2] - m
            y = wa[3] - m
            for t in reversed(self.toasts):        # 最新的最靠近底部
                w, h = t.size
                t.place_at(x - w, y - h)
                y -= h + gap
        except Exception:
            pass

    def _flush_toasts(self):
        """把积压事件按类型合并: 同类 (如多台同时连接超时) 只弹一条"""
        pending, self._pending_toasts = self._pending_toasts, []
        if not pending:
            return
        groups, order = {}, []
        for p in pending:
            if p['kind'] not in groups:
                groups[p['kind']] = []
                order.append(p['kind'])
            groups[p['kind']].append(p)
        for kind in order:
            items = groups[kind]
            color, base = {'fail': (ERRC, '连接失败'),
                           'idle': (OKC, '空闲提醒'),
                           'busy': (WARNC, '占用恢复')}.get(kind, (ACC, '提醒'))
            if len(items) == 1:
                it = items[0]
                self._toast(kind, color, it['title'], it['lines'], it['sid'])
            else:
                # 最多显示 2 台摘要, 其余合并为「…等 x 台」→ 共 3 行
                lines = [p['summary'] for p in items[:2]]
                if len(items) > 2:
                    lines.append('…等 %d 台' % len(items))
                self._toast(kind, color, '%s · %d 台' % (base, len(items)),
                            lines, [p['sid'] for p in items if p['sid']])

    def _toast(self, kind, color, title, lines, sid=None):
        try:
            Toast(self, kind, color, title, [l for l in lines if l], sid)
        except Exception as e:
            log_event('toast_err', title, str(e))

    # ---------------- 用户操作 ----------------
    def toggle_pause(self):
        self.paused = not self.paused
        self.btn_pause.configure(text='▶ 继续' if self.paused else '⏸ 暂停')
        self.logline('监控', '已暂停轮询' if self.paused else '恢复轮询', 'info')
        for sid in self.rows:
            self.dirty.add(sid)

    def refresh_now(self):
        for ev in self.kick_events.values():
            ev.set()
        self.logline('监控', '手动刷新 (各服务器立即重查)', 'info')

    def import_vscode_ssh(self):
        """读取 VSCode Remote-SSH / OpenSSH 配置, 追加未监控的主机"""
        rows = vscode_ssh_hosts()
        if not rows:
            messagebox.showinfo(
                '导入 SSH 配置',
                '未找到可导入的主机。\n\n请检查:\n  · ~/.ssh/config 是否存在 Host 条目\n'
                '  · 或 VSCode 设置 remote.SSH.configFile 指向的文件')
            return
        existing = set()
        for r in self.cfg['servers']:
            try:
                existing.add((str(r.get('host', '')), str(r.get('user', '')),
                              int(r.get('port') or 22)))
            except (TypeError, ValueError):
                continue
        added = 0
        for r in rows:
            key = (r['host'], r.get('user') or '', int(r.get('port') or 22))
            if key in existing:
                continue
            r['id'] = self._new_id()
            r['enabled'] = True
            self.cfg['servers'].append(r)
            existing.add(key)
            added += 1
        save_cfg(self.cfg)
        self.rebuild_rows()
        self._restart_workers()
        self.logline('导入', 'VSCode/OpenSSH 配置: 新增 %d 台 (配置共 %d 条)' % (
            added, len(rows)), 'info')
        if added == 0:
            messagebox.showinfo('导入 SSH 配置', '配置中的主机均已存在，无新增。')

    def _show_test_result(self, sid, snap):
        row = self._row_cfg(sid)
        name = (row or {}).get('label') or (snap.get('hostname') or sid)
        if snap.get('ok'):
            messagebox.showinfo(
                '测试连接 · %s' % name,
                '连通 OK   %.1fs\n主机名: %s\nNPU: %d 卡\nCore %s · HBM %s/%sG' % (
                    snap['elapsed'], snap.get('hostname', '-'),
                    len(snap.get('chips', [])), fmt_pct(snap.get('core_avg')),
                    fmt_gb(snap.get('hbm_used')), fmt_gb(snap.get('hbm_total'))))
        else:
            messagebox.showerror('测试连接 · %s' % name,
                                 '失败: %s' % (snap.get('error') or '未知错误'))

    def test_row(self, sid):
        row = self._row_cfg(sid)
        if row is None:
            return
        self.logline((row.get('label') or row['host']), '正在测试连接…', 'info')

        def work():
            try:
                s = demo_fetch(row, self.cfg) if self.demo else fetch_one(row, self.cfg)
            except Exception as e:
                s = {'ok': False, 'error': '测试异常: %s' % e, 'hostname': '',
                     'chips': [], 'ts': time.time(), 'elapsed': 0.0}
            self.q.put(('test', sid, s))
        threading.Thread(target=work, daemon=True, name='test-%s' % sid).start()

    # ---------------- 服务器 增/改/删 ----------------
    def _new_id(self):
        self._id_seed += 1
        return 's%d' % self._id_seed

    def add_row(self):
        self._server_dialog(None)

    def edit_row(self, sid):
        row = self._row_cfg(sid)
        if row is not None:
            self._server_dialog(row)

    def del_row(self, sid):
        row = self._row_cfg(sid)
        if row is None:
            return
        if not messagebox.askyesno('删除服务器', '确定删除 %s (%s) 的监控?' % (
                row.get('label') or row['host'], row['host'])):
            return
        self.cfg['servers'] = [r for r in self.cfg['servers'] if r['id'] != sid]
        save_cfg(self.cfg)
        self.rebuild_rows()
        self._restart_workers()
        self.logline('删除', row['host'], 'info')

    def _server_dialog(self, row):
        is_new = row is None
        s = max(1.0, self.scale)
        dlg = tk.Toplevel(self.root)
        dlg.title('添加服务器' if is_new else '编辑服务器')
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        win11_titlebar(dlg)

        v_label = tk.StringVar(value=(row or {}).get('label', ''))
        v_host  = tk.StringVar(value=(row or {}).get('host', ''))
        v_user  = tk.StringVar(value=(row or {}).get('user', 'root'))
        v_port  = tk.StringVar(value=str((row or {}).get('port', 22) or 22))
        v_en    = tk.BooleanVar(value=bool((row or {}).get('enabled', True)))
        v_core  = tk.StringVar(value=str((row or {}).get('idle_core') or ''))
        v_hbm   = tk.StringVar(value=str((row or {}).get('idle_hbm_gb') or ''))
        v_cyc   = tk.StringVar(value=str((row or {}).get('idle_cycles') or ''))
        g = self.cfg['defaults']

        def header(r, title):
            tk.Label(dlg, text=title, font=F_HDB, bg=BG, fg=TXT).grid(
                row=r, column=0, columnspan=2, sticky='w',
                pady=(int(10 * s), int(2 * s)))

        def lbl(r, text):
            tk.Label(dlg, text=text, font=F_SM, bg=BG, fg=DIM, anchor='e').grid(
                row=r, column=0, sticky='e', padx=(0, int(10 * s)), pady=int(5 * s))

        def mkentry(r, var, width=30):
            e = tk.Entry(dlg, textvariable=var, font=F_NUM, bg=CARD2, fg=TXT,
                         insertbackground=TXT, relief='flat', width=width, bd=0,
                         highlightthickness=1, highlightbackground=LINE,
                         highlightcolor=ACC)
            e.grid(row=r, column=1, sticky='w', pady=int(5 * s), ipady=int(3 * s))
            return e

        header(0, '基本信息')
        lbl(1, '标签 (可选)')
        ent_label = mkentry(1, v_label)
        lbl(2, '主机 / IP *')
        ent_host = mkentry(2, v_host)
        lbl(3, '用户名')
        ent_user = mkentry(3, v_user, 14)
        lbl(4, '端口')
        ent_port = mkentry(4, v_port, 8)
        tk.Checkbutton(dlg, text='启用监控', variable=v_en, bg=BG, fg=TXT, font=F_SM,
                       activebackground=BG, activeforeground=TXT,
                       selectcolor=CARD2, bd=0, highlightthickness=0,
                       cursor='hand2').grid(row=5, column=1, sticky='w',
                                            pady=int(5 * s))

        header(6, '占用判定 (留空 = 全局默认)')
        lbl(7, '阈值')
        tf = tk.Frame(dlg, bg=BG)
        tf.grid(row=7, column=1, sticky='w', pady=int(5 * s))
        tk.Label(tf, text='Core ≥', bg=BG, fg=DIM, font=F_XS).pack(side='left')
        sp_core = tk.Spinbox(tf, textvariable=v_core, from_=0, to=100, width=5,
                             font=F_NUM, bg=CARD2, fg=TXT, insertbackground=TXT,
                             relief='flat', justify='center', bd=0,
                             buttonbackground=CARD2, highlightthickness=1,
                             highlightbackground=LINE, highlightcolor=ACC)
        sp_core.pack(side='left', padx=(2, 6))
        tk.Label(tf, text='%   HBM ≥', bg=BG, fg=DIM, font=F_XS).pack(side='left')
        sp_hbm = tk.Spinbox(tf, textvariable=v_hbm, from_=0, to=4096, width=5,
                            font=F_NUM, bg=CARD2, fg=TXT, insertbackground=TXT,
                            relief='flat', justify='center', bd=0,
                            buttonbackground=CARD2, highlightthickness=1,
                            highlightbackground=LINE, highlightcolor=ACC)
        sp_hbm.pack(side='left', padx=(2, 6))
        tk.Label(tf, text='GB   连续', bg=BG, fg=DIM, font=F_XS).pack(side='left')
        sp_cyc = tk.Spinbox(tf, textvariable=v_cyc, from_=1, to=100, width=4,
                            font=F_NUM, bg=CARD2, fg=TXT, insertbackground=TXT,
                            relief='flat', justify='center', bd=0,
                            buttonbackground=CARD2, highlightthickness=1,
                            highlightbackground=LINE, highlightcolor=ACC)
        sp_cyc.pack(side='left', padx=(2, 4))
        tk.Label(tf, text='轮空闲', bg=BG, fg=DIM, font=F_XS).pack(side='left')
        tk.Label(dlg, text='留空=全局默认 (Core>%s%% / HBM>%sG / %d 轮); 填 0 表示只要非 0 即占用' % (
            g.get('idle_core'), g.get('idle_hbm_gb'), int(ov(None, self.cfg, 'idle_cycles'))),
            font=F_XS, bg=BG, fg=DIM, justify='left').grid(
            row=8, column=1, sticky='w', pady=(0, 4))

        test_lbl = tk.Label(dlg, text='', font=F_SM, bg=BG, fg=DIM, anchor='w')
        test_lbl.grid(row=9, column=0, columnspan=2, sticky='w', pady=(4, 2))

        btns = tk.Frame(dlg, bg=BG)
        btns.grid(row=10, column=0, columnspan=2, sticky='ew',
                  padx=int(22 * s), pady=(int(10 * s), 0))

        def do_test():
            rowtmp = {
                'id': (row or {}).get('id', 'tmp'),
                'label': v_label.get().strip(),
                'host': v_host.get().strip(),
                'user': v_user.get().strip(),
                'port': v_port.get().strip() or 22,
                'idle_core': v_core.get().strip(),
                'idle_hbm_gb': v_hbm.get().strip(),
                'idle_cycles': v_cyc.get().strip(),
            }
            if not rowtmp['host']:
                test_lbl.configure(text='✗ 请先填写主机 / IP', fg=ERRC)
                return
            btn_test.configure(state='disabled', text='测试中…')
            test_lbl.configure(text='测试中…', fg=DIM)
            box = {}

            def run():
                try:
                    box['s'] = (demo_fetch(rowtmp, self.cfg) if self.demo
                                else fetch_one(rowtmp, self.cfg))
                except Exception as e:
                    box['s'] = {'ok': False, 'error': '测试异常: %s' % e}
            threading.Thread(target=run, daemon=True).start()

            def poll():
                if 's' in box:
                    s = box['s']
                    btn_test.configure(state='normal', text='测试连接')
                    if s.get('ok'):
                        test_lbl.configure(
                            text='✓ OK %.1fs · %d 卡 · Core %s · 主机 %s' % (
                                s['elapsed'], len(s.get('chips', [])),
                                fmt_pct(s.get('core_avg')), s.get('hostname', '?')),
                            fg=OKC)
                    else:
                        test_lbl.configure(
                            text='✗ %s' % (s.get('error', '失败').replace('\n', ' | ')[:160]),
                            fg=ERRC)
                else:
                    dlg.after(300, poll)
            dlg.after(300, poll)

        def do_save():
            host = v_host.get().strip()
            if not v_en.get() and not host:
                v_en.set(True)
            if not host:
                messagebox.showwarning('缺信息', '主机 / IP 不能为空', parent=dlg)
                return
            try:
                port = int(v_port.get().strip() or 22)
                if not (1 <= port <= 65535):
                    raise ValueError
            except ValueError:
                messagebox.showwarning('端口错误', '端口应为 1~65535 的数字', parent=dlg)
                return
            newrow = {
                'id': (row or {}).get('id') or self._new_id(),
                'label': v_label.get().strip(),
                'host': host,
                'user': v_user.get().strip() or 'root',
                'port': port,
                'enabled': bool(v_en.get()),
                'idle_core': v_core.get().strip(),
                'idle_hbm_gb': v_hbm.get().strip(),
                'idle_cycles': v_cyc.get().strip(),
            }
            if not is_new:
                for i, r2 in enumerate(self.cfg['servers']):
                    if r2['id'] == row['id']:
                        self.cfg['servers'][i] = newrow
                        break
            else:
                self.cfg['servers'].append(newrow)
            save_cfg(self.cfg)
            self.rebuild_rows()
            self._restart_workers()
            self.logline('服务器', '%s %s [%s]' % ('更新' if not is_new else '添加',
                                                  host, newrow['id']), 'info')
            dlg.destroy()

        btn_test = tk.Button(btns, text='测试连接', font=F_SM, bd=0, relief='flat',
                             bg=CARD2, fg=ACC, activebackground=LINE,
                             activeforeground=TXT, padx=int(12 * s), pady=int(5 * s),
                             cursor='hand2', command=do_test)
        btn_test.pack(side='left')
        hover_btn(btn_test, CARD2, LINE, ACC, TXT)
        right = tk.Frame(btns, bg=BG)
        right.pack(side='right')
        b_cancel = tk.Button(right, text='取消', font=F_SM, bd=0, relief='flat',
                             bg=CARD2, fg=TXT, activebackground=LINE,
                             activeforeground=TXT, padx=int(14 * s), pady=int(5 * s),
                             cursor='hand2', command=dlg.destroy)
        b_cancel.pack(side='left', padx=(0, int(8 * s)))
        b_ok = tk.Button(right, text='保存', font=F_SM, bd=0, relief='flat',
                         bg=ACC, fg='#101418', activebackground='#79d2ff',
                         activeforeground='#101418', padx=int(16 * s), pady=int(5 * s),
                         cursor='hand2', command=do_save)
        b_ok.pack(side='left')
        hover_btn(b_ok, ACC, '#79d2ff')

        dlg.bind('<Return>', lambda e: do_save())
        dlg.bind('<Escape>', lambda e: dlg.destroy())
        dlg.update_idletasks()
        try:                                   # 居中于主窗口
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            dw, dh = dlg.winfo_width(), dlg.winfo_height()
            dlg.geometry('+%d+%d' % (max(0, rx + (rw - dw) // 2),
                                     max(0, ry + (rh - dh) // 3)))
        except Exception:
            dlg.geometry('+%d+%d' % (self.root.winfo_rootx() + 120,
                                     self.root.winfo_rooty() + 120))

    # ---------------- 设置 ----------------
    def open_settings(self):
        s = max(1.0, self.scale)
        dlg = tk.Toplevel(self.root)
        dlg.title('设置')
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        win11_titlebar(dlg)
        g = self.cfg

        v_int = tk.StringVar(value=str(g.get('interval', 15)))
        v_ct  = tk.StringVar(value=str(g.get('connect_timeout', 5)))
        v_et  = tk.StringVar(value=str(g.get('exec_timeout', 25)))
        v_rn  = tk.StringVar(value=str(g.get('renotify_min', 30)))
        v_dc  = tk.StringVar(value=str(g['defaults'].get('idle_core', 5)))
        v_dh  = tk.StringVar(value=str(g['defaults'].get('idle_hbm_gb', 8)))
        v_dcy = tk.StringVar(value=str(g['defaults'].get('idle_cycles', 3)))
        v_snd = tk.BooleanVar(value=bool(g.get('sound', True)))
        v_aid = tk.BooleanVar(value=bool(g.get('alert_idle', True)))
        v_arc = tk.BooleanVar(value=bool(g.get('alert_recover', False)))
        v_afl = tk.BooleanVar(value=bool(g.get('alert_fail', True)))

        tk.Label(dlg, text='设置', font=('Microsoft YaHei UI', 14, 'bold'),
                 bg=BG, fg=TXT, anchor='w').pack(
            fill='x', padx=int(22 * s), pady=(int(18 * s), int(4 * s)))

        body = tk.Frame(dlg, bg=BG)
        body.pack(fill='x', padx=int(22 * s))
        body.columnconfigure(1, minsize=int(70 * s))
        body.columnconfigure(3, minsize=int(70 * s))

        def section(r, title):
            tk.Label(body, text=title, font=F_HDB, bg=BG, fg=TXT).grid(
                row=r, column=0, columnspan=4, sticky='w',
                pady=(int(12 * s), int(2 * s)))
            return r + 1

        def lbl(r, c, text):
            tk.Label(body, text=text, font=F_SM, bg=BG, fg=DIM, anchor='e').grid(
                row=r, column=c, sticky='e', padx=(0, int(8 * s)), pady=int(5 * s))

        def spin(r, c, var, lo, hi, width=6):
            tk.Spinbox(body, textvariable=var, from_=lo, to=hi, width=width,
                       font=F_NUM, bg=CARD2, fg=TXT, insertbackground=TXT,
                       relief='flat', justify='center', bd=0,
                       buttonbackground=CARD2, highlightthickness=1,
                       highlightbackground=LINE, highlightcolor=ACC).grid(
                row=r, column=c, sticky='w', pady=int(5 * s))

        def chk(r, c, text, var):
            tk.Checkbutton(body, text=text, variable=var, bg=BG, fg=TXT, font=F_SM,
                           activebackground=BG, activeforeground=TXT,
                           selectcolor=CARD2, bd=0, highlightthickness=0,
                           cursor='hand2').grid(row=r, column=c, columnspan=2,
                                                sticky='w', pady=int(4 * s))

        r = section(0, '轮询采集')
        lbl(r, 0, '轮询间隔 (秒)');      spin(r, 1, v_int, 3, 3600)
        lbl(r, 2, '连接超时 (秒)');      spin(r, 3, v_ct, 1, 60)
        r += 1
        lbl(r, 0, '执行超时 (秒)');      spin(r, 1, v_et, 3, 300)
        lbl(r, 2, '空闲重复提醒 (分)');  spin(r, 3, v_rn, 0, 1440)
        r += 1

        r = section(r + 1, '占用判定 (全局默认, 可按服务器覆盖)')
        lbl(r, 0, 'Core > (%)');        spin(r, 1, v_dc, 0, 100)
        lbl(r, 2, 'HBM > (GB)');        spin(r, 3, v_dh, 0, 4096)
        r += 1
        lbl(r, 0, '连续空闲确认 (轮)'); spin(r, 1, v_dcy, 1, 100)
        r += 1
        tk.Label(body, text='任一卡 Core>T% 或 HBM>TG → 占用 (T=0 表示非 0 即占用);\n'
                            '全部卡低于阈值且连续 N 轮空闲 → 弹空闲提醒',
                 font=F_XS, bg=BG, fg=DIM, justify='left').grid(
            row=r, column=0, columnspan=4, sticky='w', pady=(int(2 * s), 0))
        r += 1

        r = section(r + 1, '提醒')
        chk(r, 0, '空闲提醒', v_aid)
        chk(r, 2, '恢复占用提醒', v_arc)
        r += 1
        chk(r, 0, '连接失败提醒', v_afl)
        chk(r, 2, '提示音', v_snd)
        r += 1

        def do_reset():
            d = DEFAULT_CFG
            v_int.set(str(d['interval']));  v_ct.set(str(d['connect_timeout']))
            v_et.set(str(d['exec_timeout'])); v_rn.set(str(d['renotify_min']))
            v_dc.set(str(d['defaults']['idle_core']))
            v_dh.set(str(d['defaults']['idle_hbm_gb']))
            v_dcy.set(str(d['defaults']['idle_cycles']))
            v_snd.set(bool(d['sound'])); v_aid.set(bool(d['alert_idle']))
            v_arc.set(bool(d['alert_recover'])); v_afl.set(bool(d['alert_fail']))

        def do_ok():
            try:
                self.cfg['interval'] = max(3, int(v_int.get()))
            except ValueError:
                pass
            for k, var in (('connect_timeout', v_ct), ('exec_timeout', v_et),
                           ('renotify_min', v_rn)):
                try:
                    self.cfg[k] = max(0, int(var.get()))
                except ValueError:
                    pass
            for k, var in (('idle_core', v_dc), ('idle_hbm_gb', v_dh),
                           ('idle_cycles', v_dcy)):
                try:
                    self.cfg['defaults'][k] = float(var.get())
                except ValueError:
                    pass
            self.cfg['sound'] = bool(v_snd.get())
            self.cfg['alert_idle'] = bool(v_aid.get())
            self.cfg['alert_recover'] = bool(v_arc.get())
            self.cfg['alert_fail'] = bool(v_afl.get())
            save_cfg(self.cfg)
            self._restart_workers()
            self.logline('设置', '已保存并生效', 'info')
            dlg.destroy()

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill='x', padx=int(22 * s), pady=(int(14 * s), int(18 * s)))
        b_reset = tk.Button(btns, text='恢复默认', font=F_SM, bd=0, relief='flat',
                            bg=CARD2, fg=DIM, activebackground=CARD2,
                            activeforeground=TXT, padx=int(12 * s), pady=int(5 * s),
                            cursor='hand2', command=do_reset)
        b_reset.pack(side='left')
        hover_btn(b_reset, CARD2, LINE, DIM, TXT)
        right = tk.Frame(btns, bg=BG)
        right.pack(side='right')
        b_cancel = tk.Button(right, text='取消', font=F_SM, bd=0, relief='flat',
                             bg=CARD2, fg=TXT, activebackground=LINE,
                             activeforeground=TXT, padx=int(14 * s), pady=int(5 * s),
                             cursor='hand2', command=dlg.destroy)
        b_cancel.pack(side='left', padx=(0, int(8 * s)))
        b_ok = tk.Button(right, text='保存', font=F_SM, bd=0, relief='flat',
                         bg=ACC, fg='#101418', activebackground='#79d2ff',
                         activeforeground='#101418', padx=int(16 * s), pady=int(5 * s),
                         cursor='hand2', command=do_ok)
        b_ok.pack(side='left')
        hover_btn(b_ok, ACC, '#79d2ff')

        dlg.bind('<Escape>', lambda e: dlg.destroy())
        dlg.update_idletasks()
        try:                                   # 居中于主窗口
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            dw, dh = dlg.winfo_width(), dlg.winfo_height()
            dlg.geometry('+%d+%d' % (max(0, rx + (rw - dw) // 2),
                                     max(0, ry + (rh - dh) // 3)))
        except Exception:
            dlg.geometry('+%d+%d' % (self.root.winfo_rootx() + 120,
                                     self.root.winfo_rooty() + 90))

    # ---------------- 卡片详情 ----------------
    def chip_detail(self, sid, chip):
        row = self._row_cfg(sid)
        snap = self.last.get(sid) or {}
        name = (row or {}).get('label') or snap.get('hostname') or sid
        dlg = tk.Toplevel(self.root)
        dlg.title('NPU 详情 · %s' % name)
        dlg.configure(bg=CARD, padx=int(22 * max(1.0, self.scale)),
                      pady=int(16 * max(1.0, self.scale)))
        dlg.transient(self.root)
        dlg.grab_set()
        win11_titlebar(dlg)

        def L(r, txt, col=DIM, font=F_SM, anchor='e', colidx=1):
            tk.Label(dlg, text=txt, font=font, bg=CARD, fg=col, anchor=anchor,
                     justify='left').grid(row=r, column=colidx, sticky='nw', padx=4,
                                          pady=2)

        tk.Label(dlg, text='服务器', font=F_SM, bg=CARD, fg=DIM, anchor='e').grid(
            row=0, column=0, sticky='e', padx=4, pady=2)
        L(0, '%s  (%s)' % (name, row['host'] if row else '?'), col=TXT)
        tk.Label(dlg, text='NPU 卡号 / 组号', font=F_SM, bg=CARD, fg=DIM, anchor='e').grid(
            row=1, column=0, sticky='e', padx=4, pady=2)
        L(1, 'card %s · group %s · PhyID %s' % (
            chip.get('npu', '?'), chip.get('grp', '?'), chip.get('chip', '?')), col=TXT)
        tk.Label(dlg, text='PCIe BUS ID', font=F_SM, bg=CARD, fg=DIM, anchor='e').grid(
            row=2, column=0, sticky='e', padx=4, pady=2)
        L(2, chip.get('bus', '') or '-', col=TXT, font=F_NUM)
        tk.Label(dlg, text='型号', font=F_SM, bg=CARD, fg=DIM, anchor='e').grid(
            row=3, column=0, sticky='e', padx=4, pady=2)
        L(3, chip.get('name') or '-', col=TXT)
        tk.Label(dlg, text='健康', font=F_SM, bg=CARD, fg=DIM, anchor='e').grid(
            row=4, column=0, sticky='e', padx=4, pady=2)
        hcol = OKC if str(chip.get('health', '')).upper() in ('OK', '') else WARNC
        L(4, str(chip.get('health', '')) or 'OK', col=hcol)
        tk.Label(dlg, text='AICore 利用率', font=F_SM, bg=CARD, fg=DIM, anchor='e').grid(
            row=5, column=0, sticky='e', padx=4, pady=2)
        L(5, fmt_pct(chip.get('aicore')), col=TXT, font=F_HDB)
        tk.Label(dlg, text='HBM 显存', font=F_SM, bg=CARD, fg=DIM, anchor='e').grid(
            row=6, column=0, sticky='e', padx=4, pady=2)
        L(6, '%s MB / %s MB' % (chip.get('hbm_used') or 0, chip.get('hbm_total') or 0),
           col=TXT, font=F_NUM)
        tk.Label(dlg, text='内存 (DDR)', font=F_SM, bg=CARD, fg=DIM, anchor='e').grid(
            row=7, column=0, sticky='e', padx=4, pady=2)
        L(7, '%s MB / %s MB' % (chip.get('mem_used') or 0, chip.get('mem_total') or 0),
           col=TXT, font=F_NUM)
        tk.Label(dlg, text='温度 / 功耗', font=F_SM, bg=CARD, fg=DIM, anchor='e').grid(
            row=8, column=0, sticky='e', padx=4, pady=2)
        L(8, '%s °C   /   %s W' % (
            chip.get('temp') if chip.get('temp') is not None else '--',
            chip.get('power') if chip.get('power') is not None else '--'),
            col=TXT, font=F_NUM)
        tk.Label(dlg, text='占用判定', font=F_SM, bg=CARD, fg=DIM, anchor='e').grid(
            row=9, column=0, sticky='e', padx=4, pady=2)
        st_col = WARNC if chip.get('_busy') else OKC
        L(9, '占用中' if chip.get('_busy') else '空闲', col=st_col, font=F_HDB)

        dlg.bind('<Escape>', lambda e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry('+%d+%d' % (self.root.winfo_rootx() + 260,
                                 self.root.winfo_rooty() + 180))

    # ---------------- 悬浮小窗 ----------------
    def open_mini(self):
        if self.mini is not None:
            return
        try:
            self.mini = MiniWidget(self)
            self.btn_mini.configure(text='▦ 悬浮窗 ✓')
            self.logline('悬浮窗', '已打开 (置顶, 可拖动)', 'info')
        except Exception as e:
            self.logline('悬浮窗', '打开失败: %s' % e, 'fail')

    def close_mini(self):
        if self.mini is None:
            return
        try:
            ui = self.cfg.setdefault('ui', {})
            m = ui.setdefault('mini', {})
            m['visible'] = False
            m['geometry'] = '+%d+%d' % self.mini._pos
            save_cfg(self.cfg)
            self.mini._tip_hide()
            self.mini.top.destroy()
        except Exception:
            pass
        self.mini = None
        self.btn_mini.configure(text='▦ 悬浮窗')

    def toggle_mini(self):
        if self.mini is None:
            ui = self.cfg.setdefault('ui', {})
            ui.setdefault('mini', {})['visible'] = True
            save_cfg(self.cfg)
            self.open_mini()
        else:
            self.close_mini()

    def open_chip_from_mini(self, sid, i):
        snap = self.last.get(sid)
        if not (snap and snap.get('ok') and i < len(snap.get('chips', []))):
            return
        try:
            if self.root.state() == 'iconic':
                self.root.deiconify()
        except Exception:
            pass
        self.chip_detail(sid, snap['chips'][i])

    # ---------------- 关闭 ----------------
    def on_close(self):
        if self.mini is not None:
            try:
                ui = self.cfg.setdefault('ui', {})
                m = ui.setdefault('mini', {})
                m['visible'] = True
                m['geometry'] = '+%d+%d' % self.mini._pos
            except Exception:
                pass
        if not self.demo:          # 演示/冒烟模式绝不覆盖真实配置
            try:
                self.cfg['ui'] = {'geometry': self.root.winfo_geometry()}
                save_cfg(self.cfg)
            except Exception:
                pass
        self.stop.set()
        try:
            self.root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
EMBED_SAMPLE = '''
hostname: demo
+------------------------------------------------------------------------------------------------------------------+
| npu-smi 26.1.1                              Version: 26.1.1                                                      |
+---------------------------+---------------+----------------------------------------------------------------------+
| NPU   Name                | Health        | Power(W)             Temp(C)                 Hugepages-Usage(page)   |
| Chip  Phy-ID              | Bus-Id        | AICore(%)            Memory-Usage(MB)        HBM-Usage(MB)           |
+===========================+===============+======================================================================+
| 0     Ascend910           | OK            | 166.3                41                      0    / 0                |
| 0     0                   | 0000:AE:00.0  | 0                    0    / 0                60166/ 65536            |
+------------------------------------------------------------------------------------------------------------------+
| 0     Ascend910           | OK            | -                    42                      0    / 0                |
| 1     1                   | 0000:95:00.0  | 0                    0    / 0                2862 / 65536            |
+------------------------------------------------------------------------------------------------------------------+
'''


def selftest():
    ok = True
    samples = {}
    for name in ('probe_174.txt', 'probe_197.txt'):
        p = os.path.join(APP_DIR, name)
        if os.path.isfile(p):
            try:
                raw = open(p, 'rb').read()
                if raw.startswith(b'\xff\xfe'):          # UTF-16 BOM (PowerShell 旧探测文件)
                    txt = raw.decode('utf-16-le', errors='replace')
                else:
                    txt = raw.decode('utf-8', errors='replace')
                samples[name] = txt
            except Exception:
                pass
    samples['embedded'] = EMBED_SAMPLE

    for tag, txt in samples.items():
        _head, mark, body = txt.partition('__NPU_END__')
        if not mark:
            body = txt             # 旧探测文件没有该标记: 兜底解析全文
        chips = parse_npu_smi(body)
        bad = [c for c in chips if c.get('aicore') is None or c.get('hbm_used') is None]
        print('%s: %d chips' % (tag, len(chips)))
        if bad or not chips:
            ok = False
            print('  解析异常样例:', (bad or chips)[:1])
        else:
            c = chips[0]
            print('  chip0: npu=%s phy=%s core=%s hbm=%s/%s temp=%s power=%s health=%s' % (
                c.get('npu'), c.get('chip'), c.get('aicore'),
                c.get('hbm_used'), c.get('hbm_total'), c.get('temp'),
                c.get('power'), c.get('health')))
    print('SELFTEST %s' % ('OK' if ok else 'FAILED'))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    args = set(sys.argv[1:])
    if '--selftest' in args:
        return selftest()
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    app = MonitorApp(root, demo=('--demo' in args) or ('--smoke' in args))
    if '--smoke' in args:
        def smoke_toast():                 # 冒烟: 验证同类事件合并 (2条fail → 1条弹窗)
            now = time.time()
            app._pending_toasts.extend([
                {'kind': 'fail', 'ts': now, 'sid': 's1', 'name': '演示-A',
                 'title': '连接失败 · 演示-A',
                 'summary': '演示-A  连接超时 / 网络不可达',
                 'lines': ['连接超时 / 网络不可达', '目标: root@10.0.0.1']},
                {'kind': 'fail', 'ts': now, 'sid': 's2', 'name': '演示-B',
                 'title': '连接失败 · 演示-B',
                 'summary': '演示-B  连接超时 / 网络不可达',
                 'lines': ['连接超时 / 网络不可达', '目标: root@10.0.0.2']},
            ])
            app._flush_toasts()
        app.cfg['sound'] = False           # 冒烟测试不发声
        root.after(700, smoke_toast)
        root.after(1400, app.open_mini)    # 冒烟: 同时验证悬浮窗构建
        root.after(2600, app.on_close)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
