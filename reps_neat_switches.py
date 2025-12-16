import threading
import time
import random
import networkx as nx
import json
import logging
import webbrowser
from flask import Flask, Response
from flask_socketio import SocketIO, emit

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ==============================================================================
#  BACKEND (Same Logic, strictly verifying state emission)
# ==============================================================================
class REPSSwitch:
    def __init__(self, id, layer, capacity=4):
        self.id = id;
        self.layer = layer;
        self.capacity = capacity
        self.queues = {}

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
                e['valid'] = False
                threading.Timer(5.0, lambda: self._heal(e)).start()
                break

    def _heal(self, e):
        e['valid'] = True; e['val'] = random.randint(10, 99)


class NetworkSimulation:
    def __init__(self):
        self.graph = nx.Graph()
        self.nodes = {}
        self.links = []
        self.packets = []
        self.lock = threading.Lock()
        self.pkt_ctr = 0
        self.running = False
        self._build()

    def _build(self):
        # 1. Cores
        for i in range(1, 3):
            cid = f"C{i}"
            self.nodes[cid] = REPSSwitch(cid, 'core')
            self.graph.add_node(cid, type='core', x=35 + (i - 1) * 30, y=15)

        # 2. Pods
        for p in range(2):
            px = 15 if p == 0 else 65
            aggs = []
            # Aggs
            for i in range(1, 3):
                aid = f"A{p}_{i}"
                self.nodes[aid] = REPSSwitch(aid, 'agg')
                self.graph.add_node(aid, type='agg', x=px + (i - 1) * 20, y=45)
                aggs.append(aid)
                # Uplinks to Cores (Agg connects to all Cores)
                for cidx in range(1, 3):
                    cid = f"C{cidx}"
                    # Agg Top -> Core Bottom
                    self._add_link(aid, cid, 'TOP', cidx - 1, 'BOT', i - 1 + p * 2)

            # Edges
            for i in range(1, 3):
                eid = f"E{p}_{i}"
                self.nodes[eid] = REPSSwitch(eid, 'edge')
                self.graph.add_node(eid, type='edge', x=px + (i - 1) * 20, y=75)

                # Uplinks to Aggs
                for aidx, agg in enumerate(aggs):
                    self._add_link(eid, agg, 'TOP', aidx, 'BOT', i - 1)

                # Hosts
                for h in range(3):
                    hid = f"H{p}_{i}_{h}"
                    self.nodes[hid] = REPSHost(hid)
                    self.graph.add_node(hid, type='host', x=(px + (i - 1) * 20) + (h * 5 - 5), y=92)
                    self._add_link(eid, hid, 'BOT', h, 'TOP', 0)

    def _add_link(self, u, v, uf, us, vf, vs):
        self.graph.add_edge(u, v)
        self.links.append({
            'u': u, 'v': v,
            'uf': uf, 'us': us, 'vf': vf, 'vs': vs,
            'active': 0
        })

    def step(self):
        with self.lock:
            # Traffic
            if random.random() < 0.4:
                hosts = [n for n in self.nodes.values() if isinstance(n, REPSHost)]
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
                p['pct'] += 0.025

                # Highlight Link
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
            # Nodes
            nd = {}
            for nid, node in self.nodes.items():
                m = self.graph.nodes[nid]
                d = {'x': m['x'], 'y': m['y'], 'type': m['type']}
                if isinstance(node, REPSSwitch):
                    d['q'] = node.get_load();
                    d['cap'] = node.capacity
                nd[nid] = d

            # Packets
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
    <title>REPS Analyzer</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <style>
        body { margin:0; background:#fff; font-family:'Segoe UI',sans-serif; overflow:hidden; }
        #loading { position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); font-size:20px; color:#555; }
        canvas { display:block; }
        #hud { position:absolute; top:20px; left:20px; border:1px solid #ccc; background:#f9f9f9; padding:15px; border-radius:6px; box-shadow:0 2px 5px rgba(0,0,0,0.1); }
        #controls { position:absolute; bottom:20px; right:20px; }
        button { background:#333; color:#fff; border:none; padding:10px 20px; border-radius:4px; font-weight:bold; cursor:pointer; margin-left:10px; }
    </style>
</head>
<body>
    <div id="loading">Connecting to REPS Engine...</div>
    <div id="hud" style="display:none">
        <h3 style="margin:0">REPS PROTOCOL</h3>
        <div style="font-size:12px; color:#666; margin-top:5px">FAT-TREE TOPOLOGY • ECN MARKING</div>
    </div>
    <div id="controls" style="display:none">
        <button onclick="emit('start')">START</button>
        <button onclick="emit('pause')">PAUSE</button>
    </div>
    <canvas id="c"></canvas>

    <script>
        const socket = io();
        const cvs = document.getElementById('c');
        const ctx = cvs.getContext('2d');
        const loadDiv = document.getElementById('loading');
        const hudDiv = document.getElementById('hud');
        const ctrlDiv = document.getElementById('controls');

        let W, H;
        let state = null;

        function resize() { W=window.innerWidth; H=window.innerHeight; cvs.width=W; cvs.height=H; }
        window.addEventListener('resize', resize); resize();

        socket.on('frame', d => {
            if(!state) {
                // First frame received
                loadDiv.style.display = 'none';
                hudDiv.style.display = 'block';
                ctrlDiv.style.display = 'block';
            }
            state = d;
        });

        function emit(cmd) { socket.emit('ctrl', {cmd}); }

        function getLinkPos(nodeId, face, slot) {
            if(!state || !state.nodes[nodeId]) return {x:0, y:0};

            const n = state.nodes[nodeId];
            const cx = n.x/100*W, cy = n.y/100*H;

            if(n.type === 'host') return {x: cx, y: cy-15};

            const swW = 100, swH = 60;
            // Slots: 0..3 -> Spacing
            const spacing = 20;
            const startX = cx - (1.5 * spacing);
            const ox = startX + (slot * spacing);

            if(face === 'TOP') return {x: ox, y: cy - swH/2};
            if(face === 'BOT') return {x: ox, y: cy + swH/2};
            return {x: cx, y: cy};
        }

        function draw() {
            ctx.clearRect(0,0,W,H);

            if(!state) {
                requestAnimationFrame(draw); return;
            }

            // 1. LINKS
            ctx.lineCap = 'round';
            state.links.forEach(l => {
                const p1 = getLinkPos(l.u, l.uf, l.us);
                const p2 = getLinkPos(l.v, l.vf, l.vs);

                ctx.beginPath(); ctx.moveTo(p1.x, p1.y); ctx.lineTo(p2.x, p2.y);
                if(l.active > 0) {
                    ctx.strokeStyle = '#00BFFF'; ctx.lineWidth = 4;
                } else {
                    ctx.strokeStyle = '#ccc'; ctx.lineWidth = 1.5;
                }
                ctx.stroke();

                // Port Dots
                ctx.fillStyle='#fff'; ctx.strokeStyle='#555'; ctx.lineWidth=1;
                if(state.nodes[l.u].type!=='host'){ ctx.beginPath(); ctx.arc(p1.x,p1.y,3,0,6.28); ctx.fill(); ctx.stroke(); }
                if(state.nodes[l.v].type!=='host'){ ctx.beginPath(); ctx.arc(p2.x,p2.y,3,0,6.28); ctx.fill(); ctx.stroke(); }
            });

            // 2. NODES
            Object.entries(state.nodes).forEach(([id, n]) => {
                const cx = n.x/100*W, cy = n.y/100*H;

                if(n.type !== 'host') {
                    // Switch
                    const w=100, h=60;
                    ctx.fillStyle = (n.type==='core') ? '#4682B4' : '#87CEEB';
                    ctx.strokeStyle = '#000'; ctx.lineWidth=2;
                    ctx.beginPath(); ctx.rect(cx-w/2, cy-h/2, w, h); ctx.fill(); ctx.stroke();

                    ctx.fillStyle='#fff'; ctx.font="bold 14px sans-serif";
                    ctx.textAlign="center"; ctx.fillText(id, cx-15, cy+5);

                    // Queue
                    const qx = cx+w/2 - 20;
                    ctx.fillStyle="#f0f0f0"; ctx.fillRect(qx, cy-20, 10, 40); ctx.strokeRect(qx, cy-20, 10, 40);

                    if(n.q > 0) {
                        const hFill = (n.q/n.cap)*40;
                        ctx.fillStyle = n.q>=2 ? 'red' : '#0f0';
                        ctx.fillRect(qx, cy+20-hFill, 10, hFill);
                    }
                } else {
                    // Host
                    ctx.fillStyle='#333'; ctx.fillRect(cx-15, cy-10, 30, 20);
                    ctx.fillStyle='#fff'; ctx.font="10px sans-serif"; ctx.textAlign="center";
                    ctx.fillText(id.split('_')[2], cx, cy+4);
                }
            });

            // 3. PACKETS
            state.packets.forEach(p => {
                // Determine positions safely
                const link = state.links.find(l => (l.u==p.u && l.v==p.v) || (l.u==p.v && l.v==p.u));
                let p1, p2;
                if(link) {
                    if(link.u == p.u) {
                        p1 = getLinkPos(p.u, link.uf, link.us);
                        p2 = getLinkPos(p.v, link.vf, link.vs);
                    } else {
                        p1 = getLinkPos(p.v, link.vf, link.vs);
                        p2 = getLinkPos(p.u, link.uf, link.us);
                    }
                } else {
                    // Fallback (should ideally never happen)
                    const n1=state.nodes[p.u], n2=state.nodes[p.v];
                    p1={x:n1.x/100*W,y:n1.y/100*H}; p2={x:n2.x/100*W,y:n2.y/100*H};
                }

                const x = p1.x + (p2.x-p1.x)*p.pct;
                const y = p1.y + (p2.y-p1.y)*p.pct;

                ctx.save(); ctx.translate(x,y);
                const w=36, h=22;
                ctx.fillStyle = p.ack ? '#f3d9fa' : (p.ecn ? '#ffe3e3' : '#fff');
                ctx.strokeStyle = p.ack ? 'purple' : (p.ecn ? 'red' : 'green');
                ctx.lineWidth=2;
                ctx.beginPath(); ctx.rect(-w/2, -h/2, w, h); ctx.fill(); ctx.stroke();

                ctx.fillStyle='#000'; ctx.font="bold 10px sans-serif"; 
                ctx.textAlign="center"; ctx.fillText(p.ev, 0, 4);
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


def loop():
    while True:
        if sim.running: sim.step()
        socketio.emit('frame', sim.get_state())
        time.sleep(0.02)


if __name__ == '__main__':
    threading.Thread(target=loop, daemon=True).start()
    webbrowser.open("http://127.0.0.1:5000")
    socketio.run(app, port=5000, allow_unsafe_werkzeug=True)
