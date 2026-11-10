"""Coarse labelled ASCII map of the board: region grid, big-part labels, congestion shading.

    print(render(topo, width=100, height=30))

Shading (inside each region) is the region's load = ratsnest connections passing through it +
3 x ratsnest crossings inside it, bucketed by rank: ' ' none, '.' low ... '#' highest.
"""
SHADES = " .:+#"


def render(topo, width=100, height=30):
    b = topo.board
    x0, y0, x1, y1 = b.bbox
    sx, sy = (x1 - x0) / width, (y1 - y0) / height
    load = topo.region_load()
    score = {r: v["conns"] + 3 * v["cross"] for r, v in load.items()}
    vals = sorted({s for s in score.values() if s > 0})
    def level(s):
        if s <= 0 or not vals:
            return 0
        return 1 + min(3, int(4 * vals.index(s) / len(vals)))
    grid = [[" "] * width for _ in range(height)]
    owner = [[None] * width for _ in range(height)]
    for j in range(height):
        for i in range(width):
            owner[j][i] = topo.region_at((x0 + (i + 0.5) * sx, y0 + (j + 0.5) * sy)).id
            grid[j][i] = SHADES[level(score[owner[j][i]])]
    # borders
    for j in range(height):
        for i in range(width):
            r = owner[j][i]
            right = owner[j][i + 1] if i + 1 < width else r
            down = owner[j + 1][i] if j + 1 < height else r
            if right != r:
                grid[j][i] = "|"
            if down != r:
                grid[j][i] = "+" if grid[j][i] == "|" else "-"
    def put(i, j, text):
        for k, ch in enumerate(text):
            if 0 <= j < height and 0 <= i + k < width:
                grid[j][i + k] = ch
    # region labels (top-left inside the cell) and big parts
    for R in topo.regions.values():
        i = int((R.bbox[0] - x0) / sx) + 1
        j = int((R.bbox[1] - y0) / sy) + 1
        put(i, j, R.id)
    for ref, part in b.parts.items():
        if part.pads and part.area >= 25.0:
            bb = part.bbox
            i0, i1 = int((bb[0] - x0) / sx), int((bb[2] - x0) / sx)
            j0, j1 = int((bb[1] - y0) / sy), max(int((bb[1] - y0) / sy), int((bb[3] - y0) / sy) - 1)
            for j in range(j0, j1 + 1):
                for i in range(i0, i1 + 1):
                    if 0 <= j < height and 0 <= i < width and grid[j][i] not in "|-+":
                        grid[j][i] = "="
            put((i0 + i1) // 2 - len(ref) // 2, (j0 + j1) // 2, ref)
    lines = ["".join(r).rstrip() for r in grid]
    top = sorted(score, key=lambda r: -score[r])[:5]
    legend = "scale 1 char = %.1f x %.1f mm; shade ' .:+#' low->high load; '=' = big part (IC, connector)" % (sx, sy)
    legend += "\nbusiest: " + "  ".join("%s(conns %d, X %d)" % (r, load[r]["conns"], load[r]["cross"]) for r in top)
    return "\n".join(lines) + "\n" + legend
