# f/ION/route_drivetime — TEMP. Drive-time route sequencing via Google Distance
# Matrix (reuses f/google_maps/api_key). Per route: build a driving-duration
# matrix, order the WEEKLY stops by an exact farthest-first TSP (backbone), then
# cheapest-insert each biweekly/monthly stop so an off-week skip never reorders
# the backbone. Read-only (no ION writes). Delete when the reroute is done.
import wmill
import requests
import math
from itertools import product

OFFICE = (31.95699, -81.32371)
RM_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
CR_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


def _turn_report(api_key, pts):
    """Drive office -> pts (in order) -> office and tally turn maneuvers from the
    turn-by-turn instructions. Proves the API exposes L/R turns per route."""
    def wp(p):
        return {"location": {"latLng": {"latitude": p[0], "longitude": p[1]}}}
    body = {
        "origin": wp(pts[0]), "destination": wp(pts[0]),
        "intermediates": [wp(p) for p in pts[1:]],
        "travelMode": "DRIVE", "optimizeWaypointOrder": False,
    }
    r = requests.post(CR_URL, headers={
        "Content-Type": "application/json", "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.legs.steps.navigationInstruction.maneuver",
    }, json=body, timeout=60)
    data = r.json()
    if r.status_code != 200:
        return {"error": f"{r.status_code}: {str(data)[:180]}"}
    rt = (data.get("routes") or [{}])[0]
    tally = {}
    for leg in rt.get("legs", []):
        for st in leg.get("steps", []):
            mv = st.get("navigationInstruction", {}).get("maneuver")
            if mv:
                tally[mv] = tally.get(mv, 0) + 1
    left = sum(v for k, v in tally.items() if "LEFT" in k)
    right = sum(v for k, v in tally.items() if "RIGHT" in k)
    return {"left_turns": left, "right_turns": right, "maneuvers": tally,
            "min": round(float(str(rt.get("duration", "0s")).rstrip("s")) / 60, 1),
            "mi": round(rt.get("distanceMeters", 0) / 1609.34, 2)}


def _dur_matrix(api_key, pts):
    """Full P x P driving-duration matrix (seconds) via Routes API computeRouteMatrix.
    P<=25 keeps elements (P*P) <= 625, the per-request cap, so one request/route."""
    n = len(pts)
    def wp(p):
        return {"waypoint": {"location": {"latLng": {"latitude": p[0], "longitude": p[1]}}}}
    body = {"origins": [wp(p) for p in pts], "destinations": [wp(p) for p in pts],
            "travelMode": "DRIVE"}
    r = requests.post(RM_URL, headers={
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "originIndex,destinationIndex,duration,condition",
    }, json=body, timeout=60)
    data = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"RouteMatrix {r.status_code}: {str(data)[:200]}")
    M = [[9e5] * n for _ in range(n)]  # ROUTE_NOT_FOUND legs stay large
    for el in data:
        i, j = el["originIndex"], el["destinationIndex"]
        dur = el.get("duration")
        if dur and el.get("condition") == "ROUTE_EXISTS":
            M[i][j] = float(str(dur).rstrip("s"))
    return M


def _tsp_backbone(D, idxs):
    """Exact min-duration path over office(0)+idxs, start=farthest from office,
    end back at office. Returns ordered list of idxs (stops only)."""
    nodes = [0] + list(idxs)
    m = len(nodes)
    if m == 1:
        return []
    if m == 2:
        return [nodes[1]]
    # start = farthest stop from office by drive time
    start_local = max(range(1, m), key=lambda i: D[0][nodes[i]])
    INF = float("inf")
    FULL = 1 << m
    dp = [[INF] * m for _ in range(FULL)]
    par = [[-1] * m for _ in range(FULL)]
    # office (local index 0) is never in the visited set; the return leg is added
    # at the end. State bit k = local stop k visited.
    dp[1 << start_local][start_local] = 0.0
    for mask in range(FULL):
        if mask & 1:  # office bit should never be set
            continue
        for i in range(1, m):
            c = dp[mask][i]
            if c == INF:
                continue
            for j in range(1, m):
                if mask & (1 << j):
                    continue
                nm = mask | (1 << j)
                nc = c + D[nodes[i]][nodes[j]]
                if nc < dp[nm][j]:
                    dp[nm][j] = nc
                    par[nm][j] = i
    full = (FULL - 1) & ~1  # all stop bits set, office bit(0) clear
    best = min(range(1, m), key=lambda i: dp[full][i] + D[nodes[i]][0])
    order = []
    mask, i = full, best
    while i > 0:
        order.append(nodes[i])
        pi = par[mask][i]
        mask ^= (1 << i)
        i = pi
    order.reverse()
    return order


def _cheapest_insert(D, backbone, x):
    """Insert stop x into backbone at the position with least added drive time,
    considering the office endpoints (route is office -> backbone -> office)."""
    path = [0] + backbone + [0]
    best_pos, best_add = 1, float("inf")
    for p in range(len(path) - 1):
        a, b = path[p], path[p + 1]
        add = D[a][x] + D[x][b] - D[a][b]
        if add < best_add:
            best_add, best_pos = add, p + 1
    out = [0] + backbone + [0]
    out.insert(best_pos, x)
    return [i for i in out if i != 0]


def main(routes, office=None, turns=False):
    """routes: [{key, stops:[{id, lat, lng, weekly(bool)}]}]. Returns per-route order."""
    api_key = wmill.get_variable("f/google_maps/api_key")
    off = tuple(office) if office else OFFICE
    out = []
    for rt in routes:
        stops = rt["stops"]
        pts = [off] + [(s["lat"], s["lng"]) for s in stops]
        D = _dur_matrix(api_key, pts)
        weekly = [i + 1 for i, s in enumerate(stops) if s.get("weekly")]
        extra = [i + 1 for i, s in enumerate(stops) if not s.get("weekly")]
        if weekly:
            order = _tsp_backbone(D, weekly)
        else:  # no weekly anchor -> just solve them all
            order = _tsp_backbone(D, [i + 1 for i in range(len(stops))])
            extra = []
        for x in extra:
            order = _cheapest_insert(D, order, x)
        seq = [stops[i - 1]["id"] for i in order]
        # A/B/all drive minutes of the final order
        def mins(idxset):
            seqf = [0] + [i for i in order if i in idxset] + [0]
            return round(sum(D[seqf[k]][seqf[k + 1]] for k in range(len(seqf) - 1)) / 60.0, 1)
        allset = set(range(1, len(stops) + 1))
        rec = {"key": rt["key"], "order": seq, "drive_min_all": mins(allset), "stops": len(stops)}
        if turns:
            ordered_pts = [off] + [(stops[i - 1]["lat"], stops[i - 1]["lng"]) for i in order]
            rec["turns"] = _turn_report(api_key, ordered_pts)
        out.append(rec)
    return {"routes": out}
