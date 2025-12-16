import threading
import time
import random
import networkx as nx
import json
import logging
import webbrowser
from flask import Flask, Response, request
from flask_socketio import SocketIO, emit

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ==============================================================================
#  LAYER 1: BACKEND SIMULATION ENGINE
# ==============================================================================

class REPSSwitch:
    def __init__(self, id, layer, capacity=8):
        self.id = id
        self.layer = layer
        self.capacity = capacity
        self.queues = {}  # Map[next_hop] -> count

    def enqueue(self, next_hop):
        if next_hop not in self.queues: self.queues[next_hop] = 0
        if self.queues[next_hop] >= self.capacity: return False
        self.queues[next_hop] += 1
        return True

    def dequeue(self, next_hop):
        if next_hop in self.queues and self.queues[next_hop] > 0:
            self.queues[next_hop] -= 1

    def get_load(self):
        return sum(self.queues.values())

    def check_ecn(self):
        return self.get_load() >= 2


class REPSHost:
    def __init__(self, id):
        self.id = id
        self.ev_cache = [{'val': random.randint(10, 99), 'valid': True} for _ in range(8)]
        self.ptr = 0

    def get_valid_ev(self):
        for _ in range(8):
            idx = (self.ptr + _) % 8
            if self.ev_cache[idx]['valid']:
                self.ptr = (idx + 1) % 8;
                return self.ev_cache[idx]['val']
        return 99

    def invalidate_ev(self, ev):
        for e in self.ev_cache:
            if e['val'] == ev:
                e['valid'] = False;
                threading.Timer(5.0, lambda: self._heal(e)).start();
                break

    def _heal(self, e):
        e['valid'] = True; e['val'] = random.randint(10, 99)


class NetworkSimulation:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset_topology()  # Default config

    def reset_topology(self, config=None):
        with self.lock:
            self.graph = nx.Graph()
            self.nodes = {}
            self.links = []
            self.packets = []
            self.pkt_ctr = 0
            self.running = False

            # Default Config
            if not config:
                config = {
                    'num_pods': 2,
                    'num_cores': 2,
                    'aggs_per_pod': 2,
                    'edges_per_pod': 2,
                    'hosts_per_edge': 2
                }
            self.config = config
            self._build_dynamic_topology()

    def _build_dynamic_topology(self):
        """
        Scientifically calculates positions (0-100%) based on topology hierarchy.
        """
        C = int(self.config['num_cores'])
        P = int(self.config['num_pods'])
        A = int(self.config['aggs_per_pod'])
        E = int(self.config['edges_per_pod'])
        H = int(self.config['hosts_per_edge'])

        # --- 1. CORE LAYER (Y=10) ---
        # Spread Cores evenly across 100% width
        core_ids = []
        for i in range(C):
            cid = f"C{i + 1}"
            x_pos = (i + 0.5) * (100 / C)
            self._add_node(cid, 'core', x_pos, 10, capacity=10)
            core_ids.append(cid)

        # --- 2. POD GENERATION ---
        pod_width = 100 / P

        for p in range(P):
            # Calculate Pod X Range
            pod_x_start = p * pod_width

            agg_ids = []
            edge_ids = []

            # --- AGGREGATION LAYER (Y=40) ---
            for a in range(A):
                aid = f"A{p + 1}_{a + 1}"
                # Spread Aggs evenly within the Pod's width
                local_x = (a + 0.5) * (pod_width / A)
                abs_x = pod_x_start + local_x
                self._add_node(aid, 'agg', abs_x, 40, capacity=8)
                agg_ids.append(aid)

                # Uplinks: Connect Agg to Cores
                # Logic: We round-robin or fully mesh depending on counts.
                # Standard FatTree: Full mesh between Aggs in Pod and Cores
                for c_idx, cid in enumerate(core_ids):
                    # Agg Top Slot: c_idx
                    # Core Bottom Slot: (p * A) + a
                    self._add_link(aid, cid, 'TOP', c_idx, C, 'BOT', (p * A) + a, P * A)

            # --- EDGE LAYER (Y=70) ---
            for e in range(E):
                eid = f"E{p + 1}_{e + 1}"
                local_x = (e + 0.5) * (pod_width / E)
                abs_x = pod_x_start + local_x
                self._add_node(eid, 'edge', abs_x, 70, capacity=8)
                edge_ids.append(eid)

                # Uplinks: Connect Edge to Aggs
                for a_idx, aid in enumerate(agg_ids):
                    # Edge Top Slot: a_idx
                    # Agg Bot Slot: e
                    self._add_link(eid, aid, 'TOP', a_idx, A, 'BOT', e, E)

                # --- HOST LAYER (Y=90) ---
                for h in range(H):
                    hid = f"H{p + 1}_{e + 1}_{h + 1}"
                    # Hosts clustered under edge
                    # We give them a small sub-width under the edge switch
                    host_spread = (pod_width / E) * 0.8
                    host_start = abs_x - (host_spread / 2)
                    h_x = host_start + (h + 0.5) * (host_spread / H)

                    self._add_node(hid, 'host', h_x, 92)
                    # Downlink: Edge Bot -> Host Top
                    self._add_link(eid, hid, 'BOT', h, H, 'TOP', 0, 1)

    def _add_node(self, id, type, x, y, capacity=4):
        self.graph.add_node(id, type=type, x=x, y=y)
        if type != 'host':
            self.nodes[id] = REPSSwitch(id, type, capacity)
        else:
            self.nodes[id] = REPSHost(id)

    def _add_link(self, u, v, uf, us, u_max, vf, vs, v_max):
        """
        Adds link with Slot Metadata.
        u_max / v_max: Total ports on that face (used for centering visuals)
        """
        self.graph.add_edge(u, v)
        self.links.append({
            'u': u, 'v': v,
            'uf': uf, 'us': us, 'umax': u_max,
            'vf': vf, 'vs': vs, 'vmax': v_max,
            'active': 0
        })

    def step(self):
        with self.lock:
            # Traffic
            if random.random() < 0.5:
                hosts = [n for n in self.nodes.values() if isinstance(n, REPSHost)]
                if len(hosts) > 1:
                    src, dst = random.sample(hosts, 2)
                    ev = src.get_valid_ev()
                    try:
                        paths = list(nx.all_shortest_paths(self.graph, src.id, dst.id))
                        path = paths[ev % len(paths)]
                        self.pkt_ctr += 1
                        self.packets.append({
                            'id': self.pkt_ctr, 'type': 'DATA',
                            'src': src.id, 'dst': dst.id, 'ev': ev, 'path': path,
                            'hop': 0, 'pct': 0.0, 'ecn': False, 'ack': False, 'drop': False
                        })
                    except:
                        pass

            # Decay
            for l in self.links:
                if l['active'] > 0: l['active'] -= 1

            # Packets
            alive = []
            for p in self.packets:
                if p['drop']: continue
                p['pct'] += 0.03  # Faster visual speed

                # Highlight
                if p['hop'] < len(p['path']) - 1:
                    u, v = p['path'][p['hop']], p['path'][p['hop'] + 1]
                    for l in self.links:
                        if (l['u'] == u and l['v'] == v) or (l['u'] == v and l['v'] == u):
                            l['active'] = 2;
                            break

                if p['pct'] >= 1.0:
                    p['pct'] = 0.0
                    curr = p['path'][p['hop']]
                    target = p['src'] if p['ack'] else p['dst']

                    if curr == target:
                        if not p['ack']:
                            ack = p.copy();
                            ack['ack'] = True;
                            ack['type'] = 'ACK'
                            ack['path'] = p['path'][::-1];
                            ack['hop'] = 0;
                            ack['pct'] = 0.0
                            ack['id'] = self.pkt_ctr + 90000;
                            alive.append(ack)
                        elif p['ecn']:
                            self.nodes[p['src']].invalidate_ev(p['ev'])
                        continue

                    if p['hop'] < len(p['path']) - 1:
                        nxt = p['path'][p['hop'] + 1]

                        cn = self.nodes.get(curr)
                        if isinstance(cn, REPSSwitch): cn.dequeue(nxt)

                        nn = self.nodes.get(nxt)
                        if isinstance(nn, REPSSwitch):
                            if not nn.enqueue(curr): p['drop'] = True; continue
                            if nn.check_ecn(): p['ecn'] = True

                        p['hop'] += 1
                        alive.append(p)
                else:
                    alive.append(p)
            self.packets = alive

    def get_state(self):
        with self.lock:
            nd = {}
            for nid, node in self.nodes.items():
                m = self.graph.nodes[nid]
                d = {'x': m['x'], 'y': m['y'], 'type': m['type']}
                if isinstance(node, REPSSwitch):
                    d['q'] = node.get_load();
                    d['cap'] = node.capacity
                nd[nid] = d

            pd = []
            for p in self.packets:
                if p['drop']: continue
                u = p['path'][p['hop']]
                v = p['path'][min(p['hop'] + 1, len(p['path']) - 1)]
                pd.append({
                    'id': p['id'], 'u': u, 'v': v, 'pct': p['pct'],
                    'ev': p['ev'], 'ack': p['ack'], 'ecn': p['ecn']
                })
            return {'nodes': nd, 'links': self.links, 'packets': pd}


# ==============================================================================
#  FRONTEND
# ==============================================================================
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
sim = NetworkSimulation()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>REPS Configurable</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <style>
        body { margin:0; background:#f4f4f9; font-family:'Segoe UI',sans-serif; overflow:hidden; }
        canvas { display:block; }

        /* Config Panel */
        #config-panel {
            position:absolute; top:20px; left:20px;
            background:#fff; padding:15px; border-radius:8px;
            box-shadow:0 4px 15px rgba(0,0,0,0.1); width:200px;
            border-left: 5px solid #4682B4;
        }
        #config-panel h3 { margin:0 0 10px 0; font-size:16px; color:#333; }
        .inp-group { margin-bottom:8px; display:flex; justify-content:space-between; align-items:center; }
        label { font-size:12px; color:#555; }
        input { width:40px; padding:3px; border:1px solid #ddd; border-radius:4px; text-align:center; }

        #btn-apply {
            width:100%; background:#4682B4; color:#fff; border:none; padding:8px;
            border-radius:4px; cursor:pointer; font-weight:bold; margin-top:5px;
        }
        #btn-apply:hover { background:#315f85; }

        /* Controls */
        #controls { position:absolute; bottom:20px; right:20px; }
        .ctrl-btn { background:#333; color:#fff; border:none; padding:10px 20px; border-radius:4px; font-weight:bold; cursor:pointer; margin-left:10px; }

        /* Loading Overlay */
        #loading { position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); font-size:24px; color:#aaa; font-weight:300; }
    </style>
</head>
<body>
    <div id="loading">Connecting...</div>

    <div id="config-panel" style="display:none">
        <h3>Topology Config</h3>
        <div class="inp-group"><label>Cores</label><input id="inp-c" type="number" value="2" min="1" max="4"></div>
        <div class="inp-group"><label>Pods</label><input id="inp-p" type="number" value="2" min="1" max="4"></div>
        <div class="inp-group"><label>Aggs/Pod</label><input id="inp-a" type="number" value="2" min="1" max="4"></div>
        <div class="inp-group"><label>Edges/Pod</label><input id="inp-e" type="number" value="2" min="1" max="4"></div>
        <div class="inp-group"><label>Hosts/Edge</label><input id="inp-h" type="number" value="2" min="1" max="4"></div>
        <button id="btn-apply" onclick="applyConfig()">BUILD TOPOLOGY</button>
        <div style="margin-top:10px; font-size:10px; color:#888;">Note: High numbers may crowd screen.</div>
    </div>

    <div id="controls" style="display:none">
        <button class="ctrl-btn" onclick="emit('start')">START</button>
        <button class="ctrl-btn" onclick="emit('pause')">PAUSE</button>
    </div>

    <canvas id="c"></canvas>

    <script>
        const socket = io();
        const cvs = document.getElementById('c');
        const ctx = cvs.getContext('2d');
        let W, H;
        let state = null;

        function resize() { W=window.innerWidth; H=window.innerHeight; cvs.width=W; cvs.height=H; }
        window.addEventListener('resize', resize); resize();

        socket.on('frame', d => {
            if(!state) {
                document.getElementById('loading').style.display = 'none';
                document.getElementById('config-panel').style.display = 'block';
                document.getElementById('controls').style.display = 'block';
            }
            state = d;
        });

        function emit(cmd, val) { socket.emit('ctrl', {cmd, val}); }

        function applyConfig() {
            const cfg = {
                num_cores: document.getElementById('inp-c').value,
                num_pods: document.getElementById('inp-p').value,
                aggs_per_pod: document.getElementById('inp-a').value,
                edges_per_pod: document.getElementById('inp-e').value,
                hosts_per_edge: document.getElementById('inp-h').value
            };
            emit('reset', cfg);
        }

        // --- DYNAMIC PORT CALCULATOR ---
        function getLinkPos(nodeId, face, slot, maxSlots) {
            if(!state || !state.nodes[nodeId]) return {x:0, y:0};

            const n = state.nodes[nodeId];
            const cx = n.x/100*W, cy = n.y/100*H;

            if(n.type === 'host') return {x: cx, y: cy-15};

            const swW = 80; // Scalable switch width
            const swH = 40;

            // Calculate slot positions
            // We want to center 'maxSlots' on the face
            // Total width available is swW * 0.8 (padding)
            const availW = swW * 0.8;
            const slotW = availW / Math.max(1, maxSlots);

            // Start X (leftmost slot)
            const startX = cx - (availW / 2) + (slotW / 2);
            const ox = startX + (slot * slotW);

            if(face === 'TOP') return {x: ox, y: cy - swH/2};
            if(face === 'BOT') return {x: ox, y: cy + swH/2};
            return {x: cx, y: cy};
        }

        function draw() {
            ctx.clearRect(0,0,W,H);
            if(!state) { requestAnimationFrame(draw); return; }

            // 1. LINKS
            ctx.lineCap = 'round';
            state.links.forEach(l => {
                const p1 = getLinkPos(l.u, l.uf, l.us, l.umax);
                const p2 = getLinkPos(l.v, l.vf, l.vs, l.vmax);

                ctx.beginPath(); ctx.moveTo(p1.x, p1.y); ctx.lineTo(p2.x, p2.y);
                if(l.active > 0) {
                    ctx.strokeStyle = '#00BFFF'; ctx.lineWidth = 3;
                } else {
                    ctx.strokeStyle = '#d0d0d0'; ctx.lineWidth = 1;
                }
                ctx.stroke();

                // Ports
                ctx.fillStyle='#fff'; ctx.strokeStyle='#555'; ctx.lineWidth=1;
                if(state.nodes[l.u].type!=='host') { ctx.beginPath(); ctx.arc(p1.x,p1.y,2,0,6.28); ctx.fill(); ctx.stroke(); }
                if(state.nodes[l.v].type!=='host') { ctx.beginPath(); ctx.arc(p2.x,p2.y,2,0,6.28); ctx.fill(); ctx.stroke(); }
            });

            // 2. NODES
            Object.entries(state.nodes).forEach(([id, n]) => {
                const cx = n.x/100*W, cy = n.y/100*H;

                if(n.type !== 'host') {
                    // SWITCH
                    const w=80, h=40;
                    ctx.fillStyle = (n.type==='core') ? '#4682B4' : '#87CEEB';
                    ctx.strokeStyle = '#2c3e50'; ctx.lineWidth=2;
                    ctx.beginPath(); ctx.rect(cx-w/2, cy-h/2, w, h); ctx.fill(); ctx.stroke();

                    ctx.fillStyle='#fff'; ctx.font="bold 12px sans-serif";
                    ctx.textAlign="center"; ctx.fillText(id, cx-10, cy+5);

                    // Queue Strip (Right)
                    const qx = cx+w/2 - 12;
                    ctx.fillStyle="#f1f3f5"; ctx.fillRect(qx, cy-15, 8, 30); ctx.strokeRect(qx, cy-15, 8, 30);
                    if(n.q > 0) {
                        const hFill = Math.min(1.0, n.q/n.cap)*30;
                        ctx.fillStyle = n.q>=2 ? 'red' : '#0f0';
                        ctx.fillRect(qx, cy+15-hFill, 8, hFill);
                    }
                } else {
                    // HOST
                    ctx.fillStyle='#34495e'; ctx.fillRect(cx-12, cy-8, 24, 16);
                    ctx.fillStyle='#fff'; ctx.font="9px sans-serif"; ctx.textAlign="center";
                    ctx.fillText(id.split('_')[2], cx, cy+4);
                }
            });

            // 3. PACKETS
            state.packets.forEach(p => {
                // Find Link
                const link = state.links.find(l => (l.u==p.u && l.v==p.v) || (l.u==p.v && l.v==p.u));
                let p1, p2;
                if(link) {
                    if(link.u == p.u) {
                        p1 = getLinkPos(p.u, link.uf, link.us, link.umax);
                        p2 = getLinkPos(p.v, link.vf, link.vs, link.vmax);
                    } else {
                        p1 = getLinkPos(p.v, link.vf, link.vs, link.vmax);
                        p2 = getLinkPos(p.u, link.uf, link.us, link.umax);
                    }
                } else {
                     const n1=state.nodes[p.u], n2=state.nodes[p.v];
                     p1={x:n1.x/100*W,y:n1.y/100*H}; p2={x:n2.x/100*W,y:n2.y/100*H};
                }

                const x = p1.x + (p2.x-p1.x)*p.pct;
                const y = p1.y + (p2.y-p1.y)*p.pct;

                ctx.save(); ctx.translate(x,y);
                const w=32, h=18;
                ctx.fillStyle = p.ack ? '#f3d9fa' : (p.ecn ? '#ffe3e3' : '#fff');
                ctx.strokeStyle = p.ack ? 'purple' : (p.ecn ? 'red' : 'green');
                ctx.lineWidth=2;
                ctx.beginPath(); ctx.rect(-w/2, -h/2, w, h); ctx.fill(); ctx.stroke();

                ctx.fillStyle='#000'; ctx.font="9px sans-serif"; ctx.textAlign="center"; ctx.fillText(p.ev, 0, 3);
                ctx.fillStyle=ctx.strokeStyle; ctx.fillRect(-w/2,-h/2,w,4);
                ctx.restore();
            });

            requestAnimationFrame(draw);
        }
        requestAnimationFrame(draw);
    </script>
</body>
</html>
"""


@app.route('/')
def index(): return Response(HTML_TEMPLATE, mimetype='text/html')


@socketio.on('ctrl')
def handle_ctrl(d):
    if d['cmd'] == 'start':
        sim.running = True
    elif d['cmd'] == 'pause':
        sim.running = False
    elif d['cmd'] == 'reset':
        sim.reset_topology(d['val'])


def loop():
    while True:
        if sim.running: sim.step()
        socketio.emit('frame', sim.get_state())
        time.sleep(0.02)


if __name__ == '__main__':
    threading.Thread(target=loop, daemon=True).start()
    webbrowser.open("http://127.0.0.1:5000")
    socketio.run(app, port=5000, allow_unsafe_werkzeug=True)
