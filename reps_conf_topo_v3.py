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
    def __init__(self, id, layer, capacity=4):
        self.id = id;
        self.layer = layer
        self.capacity = capacity
        self.queues = {}

    def enqueue(self, next_hop):
        if next_hop not in self.queues: self.queues[next_hop] = 0
        if self.queues[next_hop] >= self.capacity: return False
        self.queues[next_hop] += 1
        return True

    def dequeue(self, next_hop):
        if next_hop in self.queues and self.queues[next_hop] > 0: self.queues[next_hop] -= 1

    def get_load(self):
        return sum(self.queues.values())

    def check_ecn(self):
        return self.get_load() >= (self.capacity / 2)


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
        self.reset_topology()

    def reset_topology(self, config=None):
        with self.lock:
            self.graph = nx.Graph();
            self.nodes = {};
            self.links = [];
            self.packets = [];
            self.pkt_ctr = 0;
            self.running = False
            if not config:
                config = {'num_pods': 2, 'num_cores': 2, 'aggs_per_pod': 2, 'edges_per_pod': 2, 'hosts_per_edge': 2,
                          'queue_cap': 4}
            self.config = config
            self._build_dynamic_topology()

    def _build_dynamic_topology(self):
        C = int(self.config['num_cores']);
        P = int(self.config['num_pods'])
        A = int(self.config['aggs_per_pod']);
        E = int(self.config['edges_per_pod'])
        H = int(self.config['hosts_per_edge']);
        Q = int(self.config['queue_cap'])

        # 1. Cores
        core_ids = []
        for i in range(C):
            cid = f"C{i + 1}"
            self._add_node(cid, 'core', (i + 0.5) * (100 / C), 10, capacity=Q)
            core_ids.append(cid)

        # 2. Pods
        pod_width = 100 / P
        for p in range(P):
            px = p * pod_width
            agg_ids = []
            # Aggs
            for a in range(A):
                aid = f"A{p + 1}_{a + 1}"
                self._add_node(aid, 'agg', px + (a + 0.5) * (pod_width / A), 40, capacity=Q)
                agg_ids.append(aid)
                for c_idx, cid in enumerate(core_ids):
                    self._add_link(aid, cid, 'TOP', c_idx, C, 'BOT', (p * A) + a, P * A)

            # Edges
            for e in range(E):
                eid = f"E{p + 1}_{e + 1}"
                abs_x = px + (e + 0.5) * (pod_width / E)
                self._add_node(eid, 'edge', abs_x, 70, capacity=Q)
                for a_idx, aid in enumerate(agg_ids):
                    self._add_link(eid, aid, 'TOP', a_idx, A, 'BOT', e, E)

                # Hosts
                h_spread = (pod_width / E) * 0.8;
                h_start = abs_x - (h_spread / 2)
                for h in range(H):
                    hid = f"H{p + 1}_{e + 1}_{h + 1}"
                    self._add_node(hid, 'host', h_start + (h + 0.5) * (h_spread / H), 92)
                    self._add_link(eid, hid, 'BOT', h, H, 'TOP', 0, 1)

    def _add_node(self, id, type, x, y, capacity=4):
        self.graph.add_node(id, type=type, x=x, y=y)
        if type != 'host':
            self.nodes[id] = REPSSwitch(id, type, capacity)
        else:
            self.nodes[id] = REPSHost(id)

    def _add_link(self, u, v, uf, us, u_max, vf, vs, v_max):
        self.graph.add_edge(u, v)
        # Store as TWO directional links for metrics
        self.links.append({
            'u': u, 'v': v,
            'uf': uf, 'us': us, 'umax': u_max,
            'vf': vf, 'vs': vs, 'vmax': v_max,
            'active': 0, 'bw': 10, 'util': 0.0,
        })

    def update_link_bw(self, u, v, bw):
        with self.lock:
            for l in self.links:
                if (l['u'] == u and l['v'] == v) or (l['u'] == v and l['v'] == u):
                    l['bw'] = int(bw)

    def step(self):
        with self.lock:
            # Traffic
            if random.random() < 0.6:
                hosts = [n for n in self.nodes.values() if isinstance(n, REPSHost)]
                if len(hosts) > 1:
                    src, dst = random.sample(hosts, 2)
                    ev = src.get_valid_ev()
                    try:
                        paths = list(nx.all_shortest_paths(self.graph, src.id, dst.id))
                        path = paths[ev % len(paths)]
                        self.pkt_ctr += 1
                        self.packets.append(
                            {'id': self.pkt_ctr, 'type': 'DATA', 'src': src.id, 'dst': dst.id, 'ev': ev, 'path': path,
                             'hop': 0, 'pct': 0.0, 'ecn': False, 'ack': False, 'drop': False})
                    except:
                        pass

            # Links Logic
            for l in self.links:
                if l['active'] > 0: l['active'] -= 1
                usage = (l['active'] / 5.0) * l['bw']
                l['util'] = (l['util'] * 0.9) + (usage * 0.1)

            # Packets
            alive = []
            for p in self.packets:
                if p['drop']: continue
                p['pct'] += 0.03

                # Activate specific directional link
                if p['hop'] < len(p['path']) - 1:
                    u, v = p['path'][p['hop']], p['path'][p['hop'] + 1]
                    for l in self.links:
                        # Find Exact Match U->V
                        if l['u'] == u and l['v'] == v:
                            l['active'] = 5;
                            break

                if p['pct'] >= 1.0:
                    p['pct'] = 0.0
                    curr = p['path'][p['hop']]
                    target = p['src'] if p['ack'] else p['dst']
                    if curr == target:
                        if not p['ack']:
                            ack = p.copy();
                            ack['ack'] = True;
                            ack['type'] = 'ACK';
                            ack['path'] = p['path'][::-1];
                            ack['hop'] = 0;
                            ack['pct'] = 0.0;
                            ack['id'] = self.pkt_ctr + 90000;
                            alive.append(ack)
                        elif p['ecn']:
                            self.nodes[p['src']].invalidate_ev(p['ev'])
                        continue

                    if p['hop'] < len(p['path']) - 1:
                        nxt = p['path'][p['hop'] + 1]
                        cn = self.nodes.get(curr);
                        nn = self.nodes.get(nxt)
                        if isinstance(cn, REPSSwitch): cn.dequeue(nxt)
                        if isinstance(nn, REPSSwitch):
                            if not nn.enqueue(curr): p['drop'] = True; continue
                            if nn.check_ecn(): p['ecn'] = True
                        p['hop'] += 1;
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
                if isinstance(node, REPSSwitch): d['q'] = node.get_load(); d['cap'] = node.capacity
                nd[nid] = d

            pd = []
            for p in self.packets:
                if not p['drop']:
                    try:
                        u = p['path'][p['hop']]
                        v = p['path'][min(p['hop'] + 1, len(p['path']) - 1)]
                        pd.append({'id': p['id'], 'u': u, 'v': v, 'pct': p['pct'], 'ev': p['ev'], 'ack': p['ack'],
                                   'ecn': p['ecn']})
                    except:
                        pass

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
    <title>REPS Final V4</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <style>
        body { margin:0; background:#f4f4f9; font-family:'Segoe UI',sans-serif; overflow:hidden; }
        canvas { display:block; }

        #config-panel { position:absolute; top:20px; left:20px; background:#fff; padding:15px; border-radius:8px; box-shadow:0 4px 15px rgba(0,0,0,0.1); width:200px; border-left: 5px solid #4682B4; z-index:10; }
        .inp-group { margin-bottom:8px; display:flex; justify-content:space-between; align-items:center; }
        label { font-size:12px; color:#555; }
        input { width:40px; padding:3px; border:1px solid #ddd; border-radius:4px; text-align:center; }
        button.action { width:100%; background:#4682B4; color:#fff; border:none; padding:8px; border-radius:4px; cursor:pointer; font-weight:bold; margin-top:5px; }
        button.action:hover { background:#315f85; }

        #link-modal { display:none; position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); background:#fff; padding:20px; border-radius:8px; box-shadow:0 10px 30px rgba(0,0,0,0.3); z-index:100; border:1px solid #ccc; width:220px; }
        #modal-overlay { display:none; position:absolute; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.2); z-index:90; }

        #controls { position:absolute; bottom:20px; right:20px; z-index:10; }
        .ctrl-btn { background:#333; color:#fff; border:none; padding:10px 20px; border-radius:4px; font-weight:bold; cursor:pointer; margin-left:10px; }

        #loading { position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); font-size:24px; color:#aaa; }
    </style>
</head>
<body>
    <div id="loading">Connecting...</div>

    <div id="config-panel" style="display:none">
        <h3 style="margin:0 0 10px 0; font-size:16px;">Topology Config</h3>
        <div class="inp-group"><label>Cores</label><input id="inp-c" type="number" value="2" min="1" max="4"></div>
        <div class="inp-group"><label>Pods</label><input id="inp-p" type="number" value="2" min="1" max="4"></div>
        <div class="inp-group"><label>Aggs/Pod</label><input id="inp-a" type="number" value="2" min="1" max="4"></div>
        <div class="inp-group"><label>Edges/Pod</label><input id="inp-e" type="number" value="2" min="1" max="4"></div>
        <div class="inp-group"><label>Hosts/Edge</label><input id="inp-h" type="number" value="2" min="1" max="4"></div>
        <hr style="border:0; border-top:1px solid #eee; margin:10px 0;">
        <div class="inp-group"><label>Q Cap (pkts)</label><input id="inp-q" type="number" value="4" min="2" max="20"></div>
        <button class="action" onclick="apply()">REBUILD</button>
    </div>

    <div id="controls" style="display:none">
        <button class="ctrl-btn" onclick="emit('start')">START</button>
        <button class="ctrl-btn" onclick="emit('pause')">PAUSE</button>
    </div>

    <div id="modal-overlay"></div>
    <div id="link-modal">
        <h4 style="margin:0 0 10px 0;">Configure Link</h4>
        <div class="inp-group">
            <label>BW (Gbps):</label><input id="link-bw" type="number" value="10" min="1" max="100">
        </div>
        <input type="hidden" id="lu"><input type="hidden" id="lv">
        <div style="text-align:right; margin-top:15px;">
            <button onclick="closeModal()" style="background:#ccc; border:none; padding:5px; border-radius:4px; cursor:pointer;">Cancel</button>
            <button onclick="saveLink()" style="background:#4682B4; color:#fff; border:none; padding:5px; border-radius:4px; cursor:pointer;">Save</button>
        </div>
    </div>

    <canvas id="c"></canvas>

    <script>
        const socket = io();
        const cvs = document.getElementById('c');
        const ctx = cvs.getContext('2d');
        let W, H;
        let state = null;
        let linkBoxes = [];

        function resize() { W=window.innerWidth; H=window.innerHeight; cvs.width=W; cvs.height=H; }
        window.addEventListener('resize', resize); resize();

        socket.on('frame', d => {
            if(!state) {
                document.getElementById('loading').style.display='none';
                document.getElementById('config-panel').style.display='block';
                document.getElementById('controls').style.display='block';
            }
            state = d;
        });

        function emit(cmd, val) { socket.emit('ctrl', {cmd, val}); }

        function apply() {
            emit('reset', {
                num_cores: document.getElementById('inp-c').value,
                num_pods: document.getElementById('inp-p').value,
                aggs_per_pod: document.getElementById('inp-a').value,
                edges_per_pod: document.getElementById('inp-e').value,
                hosts_per_edge: document.getElementById('inp-h').value,
                queue_cap: document.getElementById('inp-q').value
            });
        }

        cvs.addEventListener('click', e => {
            const rect = cvs.getBoundingClientRect();
            const mx = e.clientX - rect.left, my = e.clientY - rect.top;
            for(let b of linkBoxes) {
                if(pointToLineDist(mx, my, b.x1, b.y1, b.x2, b.y2) < 10) {
                    openModal(b.u, b.v, b.bw); return;
                }
            }
        });

        function pointToLineDist(x, y, x1, y1, x2, y2) {
            const A = x - x1, B = y - y1, C = x2 - x1, D = y2 - y1;
            const dot = A * C + B * D;
            const len_sq = C * C + D * D;
            let param = -1;
            if (len_sq != 0) param = dot / len_sq;
            let xx, yy;
            if (param < 0) { xx = x1; yy = y1; }
            else if (param > 1) { xx = x2; yy = y2; }
            else { xx = x1 + param * C; yy = y1 + param * D; }
            return Math.sqrt(Math.pow(x-xx, 2) + Math.pow(y-yy, 2));
        }

        function openModal(u, v, bw) {
            document.getElementById('lu').value = u;
            document.getElementById('lv').value = v;
            document.getElementById('link-bw').value = bw;
            document.getElementById('modal-overlay').style.display='block';
            document.getElementById('link-modal').style.display='block';
        }
        function closeModal() {
            document.getElementById('modal-overlay').style.display='none';
            document.getElementById('link-modal').style.display='none';
        }
        function saveLink() {
            emit('set_link_bw', {
                u: document.getElementById('lu').value,
                v: document.getElementById('lv').value,
                bw: document.getElementById('link-bw').value
            });
            closeModal();
        }

        function getLinkPos(nodeId, face, slot, maxSlots) {
            if(!state || !state.nodes[nodeId]) return {x:0, y:0};
            const n = state.nodes[nodeId];
            const cx = n.x/100*W, cy = n.y/100*H;
            if(n.type === 'host') return {x: cx, y: cy-15};
            const swW = 80, swH = 40;
            const availW = swW * 0.8;
            const slotW = availW / Math.max(1, maxSlots);
            const startX = cx - (availW / 2) + (slotW / 2);
            const ox = startX + (slot * slotW);
            if(face === 'TOP') return {x: ox, y: cy - swH/2};
            if(face === 'BOT') return {x: ox, y: cy + swH/2};
            return {x: cx, y: cy};
        }

        function draw() {
            ctx.clearRect(0,0,W,H);
            if(!state) { requestAnimationFrame(draw); return; }

            linkBoxes = [];

            // 1. LINKS
            ctx.lineCap = 'round';
            state.links.forEach(l => {
                const p1 = getLinkPos(l.u, l.uf, l.us, l.umax);
                const p2 = getLinkPos(l.v, l.vf, l.vs, l.vmax);

                // --- SEPARATION LOGIC ---
                // Heuristic: If p1.y < p2.y, it's generally "downward" in this view.
                // We shift "downward" links to the LEFT (-15), "upward" links to the RIGHT (+15).
                // Note: p1 and p2 depend on the order in links[]. The logic must be consistent per pair.

                let isDown = (p1.y < p2.y); 
                let offX = isDown ? -15 : 15;

                const d1 = {x: p1.x + offX, y: p1.y};
                const d2 = {x: p2.x + offX, y: p2.y};

                linkBoxes.push({u:l.u, v:l.v, x1:d1.x, y1:d1.y, x2:d2.x, y2:d2.y, bw:l.bw});

                ctx.beginPath(); ctx.moveTo(d1.x, d1.y); ctx.lineTo(d2.x, d2.y);
                if(l.active > 0) { ctx.strokeStyle = '#00BFFF'; ctx.lineWidth = 3; }
                else { ctx.strokeStyle = '#bbb'; ctx.lineWidth = 1.5; }
                ctx.stroke();

                // Port Circles
                ctx.fillStyle='#fff'; ctx.strokeStyle='#555'; ctx.lineWidth=1;
                if(state.nodes[l.u].type!=='host') { ctx.beginPath(); ctx.arc(d1.x,d1.y,3,0,6.28); ctx.fill(); ctx.stroke(); }
                if(state.nodes[l.v].type!=='host') { ctx.beginPath(); ctx.arc(d2.x,d2.y,3,0,6.28); ctx.fill(); ctx.stroke(); }

                // --- STAGGERED LABEL PLACEMENT ---
                // If isDown, place at 70% of the way (closer to bottom destination).
                // If !isDown (Up), place at 30% of the way (closer to top destination).
                // This physically separates the labels even further.
                const t = isDown ? 0.7 : 0.3;
                const mx = d1.x + (d2.x - d1.x) * t;
                const my = d1.y + (d2.y - d1.y) * t;

                ctx.fillStyle = 'rgba(255,255,255,0.9)'; ctx.fillRect(mx-35, my-8, 70, 16);
                ctx.strokeStyle = '#333'; ctx.strokeRect(mx-35, my-8, 70, 16);
                ctx.fillStyle = '#000'; ctx.font="bold 11px Arial"; ctx.textAlign="center";

                const pct = ((l.util / l.bw) * 100).toFixed(0);
                ctx.fillText(`${pct}% | ${l.bw}Gbps`, mx, my+4);
            });

            // 2. NODES
            Object.entries(state.nodes).forEach(([id, n]) => {
                const cx = n.x/100*W, cy = n.y/100*H;
                if(n.type !== 'host') {
                    const w=80, h=40;
                    ctx.fillStyle = (n.type==='core') ? '#4682B4' : '#87CEEB';
                    ctx.strokeStyle = '#2c3e50'; ctx.lineWidth=2;
                    ctx.beginPath(); ctx.rect(cx-w/2, cy-h/2, w, h); ctx.fill(); ctx.stroke();
                    ctx.fillStyle='#fff'; ctx.font="bold 12px sans-serif";
                    ctx.textAlign="center"; ctx.fillText(id, cx-10, cy+5);

                    const qx = cx+w/2 - 12, qH = 30;
                    ctx.fillStyle="#f1f3f5"; ctx.fillRect(qx, cy-15, 8, qH); ctx.strokeRect(qx, cy-15, 8, qH);
                    if(n.q > 0) {
                        const hFill = Math.min(1.0, n.q/n.cap)*qH;
                        ctx.fillStyle = n.q >= (n.cap/2) ? 'red' : '#0f0';
                        ctx.fillRect(qx, cy+15-hFill, 8, hFill);
                    }
                    const pct = Math.round((n.q/n.cap)*100);
                    ctx.fillStyle="#000"; ctx.font="9px Arial"; ctx.textAlign="left";
                    ctx.fillText(pct+"%", qx+12, cy+5);

                } else {
                    ctx.fillStyle='#34495e'; ctx.fillRect(cx-12, cy-8, 24, 16);
                    ctx.fillStyle='#fff'; ctx.font="9px sans-serif"; ctx.textAlign="center";
                    ctx.fillText(id.split('_')[2], cx, cy+4);
                }
            });

            // 3. PACKETS
            state.packets.forEach(p => {
                const link = state.links.find(l => l.u==p.u && l.v==p.v);
                let p1, p2;
                if(link) {
                    const l1 = getLinkPos(link.u,link.uf,link.us,link.umax);
                    const l2 = getLinkPos(link.v,link.vf,link.vs,link.vmax);

                    // Same offset logic
                    let isDown = (l1.y < l2.y); 
                    let offX = isDown ? -15 : 15;
                    p1 = {x:l1.x+offX, y:l1.y}; p2 = {x:l2.x+offX, y:l2.y};
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
                ctx.beginPath(); ctx.rect(-w/2,-h/2,w,h); ctx.fill(); ctx.stroke();
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
    elif d['cmd'] == 'set_link_bw':
        sim.update_link_bw(d['val']['u'], d['val']['v'], d['val']['bw'])


def loop():
    while True:
        if sim.running: sim.step()
        socketio.emit('frame', sim.get_state())
        time.sleep(0.02)


if __name__ == '__main__':
    threading.Thread(target=loop, daemon=True).start()
    webbrowser.open("http://127.0.0.1:5000")
    socketio.run(app, port=5000, allow_unsafe_werkzeug=True)
