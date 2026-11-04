"""Render the README's demo GIFs (chat-style replays of the MCP in action).

    uv run --no-project --with pillow python docs/images/render_demo_gifs.py docs/images

Scenes replay real tool results from live runs, with site-specific names removed. Uses macOS
system fonts (Helvetica Neue, Menlo); Pillow is only needed here, not by the package.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

S = 2  # draw at 2x, downscale for crisp text
W = 880
NAVY, BLUE, CYAN, AMBER = "#00375A", "#006EFF", "#21CEDE", "#FFBD59"
BG, LIGHT, GREY, LINE, CARD, RESULT, GREEN = (
    "#F8FAFC",
    "#E5F1FF",
    "#5C6B67",
    "#D6E4F5",
    "#FFFFFF",
    "#F1F5F9",
    "#1A9E5C",
)

SANS = "/System/Library/Fonts/HelveticaNeue.ttc"
MONO = "/System/Library/Fonts/Menlo.ttc"


def font(path, size, index=0):
    return ImageFont.truetype(path, size * S, index=index)


F_TEXT, F_BOLD = font(SANS, 15), font(SANS, 15, 1)
F_SMALL, F_TITLE = font(SANS, 12), font(SANS, 13, 1)
F_MONO, F_MONO_B = font(MONO, 12), font(MONO, 12, 1)


def wrap(draw, text, fnt, width):
    lines = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            trial = f"{line} {word}".strip()
            if draw.textlength(trial, font=fnt) <= width:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


class Scene:
    """steps: ("user", text) | ("tool", name, args, [result lines]) | ("assistant", text)."""

    def __init__(self, title, steps):
        self.title, self.steps = title, steps
        self.frames, self.durations = [], []
        probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        self.h = self._layout(probe, full=True)

    # -- drawing one state: every step up to `upto`, the last one partly (`part` 0..1, or tool phase)
    def _layout(self, d, full=False, upto=None, part=1.0, tool_done=True, img=None):
        y = 56 * S
        pad = 28 * S
        steps = self.steps if upto is None else self.steps[: upto + 1]
        for i, step in enumerate(steps):
            last = upto is not None and i == upto
            kind = step[0]
            if kind == "user":
                maxw = int(W * S * 0.68)
                lines = wrap(d, step[1], F_TEXT, maxw - 32 * S)
                words = step[1].split()
                if last and part < 1:
                    shown = " ".join(words[: max(1, int(len(words) * part))])
                    lines_draw = wrap(d, shown, F_TEXT, maxw - 32 * S)
                else:
                    lines_draw = lines
                bw = max(d.textlength(line, font=F_TEXT) for line in lines) + 32 * S
                bh = len(lines) * 22 * S + 22 * S
                x0 = W * S - pad - bw
                if img is not None:
                    img.rounded_rectangle([x0, y, W * S - pad, y + bh], 16 * S, fill=LIGHT)
                    for j, line in enumerate(lines_draw):
                        img.text((x0 + 16 * S, y + 11 * S + j * 22 * S), line, font=F_TEXT, fill=NAVY)
                y += bh + 18 * S
            elif kind == "tool":
                _, name, args, result = step
                x0, x1 = pad, W * S - pad
                arg_lines = wrap(d, args, F_MONO, x1 - x0 - 32 * S)
                n_res = len(result) if (not last or tool_done) else 0
                if last and tool_done and part < 1:
                    n_res = max(1, int(len(result) * part))
                head = 34 * S + len(arg_lines) * 18 * S
                res_h = (len(result) * 18 * S + 20 * S) if (full or not last or tool_done) else 0
                bh = head + res_h + 8 * S
                if img is not None:
                    img.rounded_rectangle([x0, y, x1, y + bh], 12 * S, fill=CARD, outline=LINE, width=S * 2)
                    img.rounded_rectangle(
                        [x0 + 14 * S, y + 12 * S, x0 + 30 * S, y + 28 * S], 4 * S, fill=BLUE
                    )
                    img.text((x0 + 40 * S, y + 12 * S), "tableau-knowledge", font=F_SMALL, fill=GREY)
                    tx = x0 + 40 * S + d.textlength("tableau-knowledge  ", font=F_SMALL)
                    img.text((tx, y + 11 * S), name, font=F_MONO_B, fill=NAVY)
                    done = (not last) or tool_done
                    status = "done" if done else "running…"
                    sx = x1 - 14 * S - d.textlength(status, font=F_SMALL)
                    img.text((sx, y + 12 * S), status, font=F_SMALL, fill=GREEN if done else AMBER)
                    for j, line in enumerate(arg_lines):
                        img.text((x0 + 16 * S, y + 34 * S + j * 18 * S), line, font=F_MONO, fill=GREY)
                    if res_h:
                        ry = y + head
                        img.rounded_rectangle([x0 + 10 * S, ry, x1 - 10 * S, ry + res_h], 8 * S, fill=RESULT)
                        for j, line in enumerate(result[:n_res]):
                            img.text((x0 + 22 * S, ry + 10 * S + j * 18 * S), line, font=F_MONO, fill=NAVY)
                y += bh + 14 * S
            elif kind == "assistant":
                maxw = W * S - 2 * pad - 8 * S
                lines = wrap(d, step[1], F_TEXT, maxw)
                if last and part < 1:
                    words = step[1].split(" ")
                    shown = " ".join(words[: max(1, int(len(words) * part))])
                    lines_draw = wrap(d, shown, F_TEXT, maxw)
                else:
                    lines_draw = lines
                if img is not None:
                    img.ellipse([pad, y + 3 * S, pad + 14 * S, y + 17 * S], fill=CYAN)
                    for j, line in enumerate(lines_draw):
                        img.text((pad + 24 * S, y + j * 23 * S), line, font=F_TEXT, fill=NAVY)
                y += len(lines) * 23 * S + 16 * S
        return y + 12 * S

    def _frame(self, upto, part=1.0, tool_done=True, ms=80):
        im = Image.new("RGB", (W * S, self.h), BG)
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, W * S, 40 * S], fill=NAVY)
        for k, c in enumerate(("#FF5F57", "#FEBC2E", "#28C840")):
            d.ellipse([16 * S + k * 20 * S, 14 * S, 28 * S + k * 20 * S, 26 * S], fill=c)
        d.text((86 * S, 12 * S), self.title, font=F_TITLE, fill="#FFFFFF")
        self._layout(d, upto=upto, part=part, tool_done=tool_done, img=d)
        self.frames.append(im.resize((W, self.h // S), Image.LANCZOS))
        self.durations.append(ms)

    def render(self, out):
        for i, step in enumerate(self.steps):
            if step[0] == "user":
                for k in range(1, 9):
                    self._frame(i, part=k / 8, ms=70)
                self._frame(i, ms=500)
            elif step[0] == "tool":
                self._frame(i, tool_done=False, ms=900)
                n = len(step[3])
                for k in range(1, n + 1):
                    self._frame(i, part=k / n, ms=140)
                self._frame(i, ms=600)
            else:
                for k in range(1, 11):
                    self._frame(i, part=k / 10, ms=90)
                self._frame(i, ms=4000)
        base = self.frames[-1].quantize(colors=240, method=Image.Quantize.MEDIANCUT).getpalette()[: 240 * 3]
        accents = [
            BG,
            CARD,
            RESULT,
            LIGHT,
            LINE,
            NAVY,
            BLUE,
            CYAN,
            AMBER,
            GREEN,
            GREY,
            "#FF5F57",
            "#FEBC2E",
            "#28C840",
        ]
        for c in accents:  # small accents lose the vote in median-cut: pin them
            base += [int(c[i : i + 2], 16) for i in (1, 3, 5)]
        pal = Image.new("P", (1, 1))
        pal.putpalette(base + [0] * (768 - len(base)))
        frames = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in self.frames]
        frames[0].save(
            out, save_all=True, append_images=frames[1:], duration=self.durations, loop=0, optimize=True
        )
        return out


SCENES = {
    "demo-add-sources.gif": Scene(
        "Claude  ·  biztory-tableau-knowledge",
        [
            ("user", "Add every published data source with “superstore” in the name to my Sales graph."),
            (
                "tool",
                "list_sources",
                'site_search="superstore", node_type="PDS"',
                [
                    "13 data sources on the site match",
                    "  Sample - Superstore            in_graph: false",
                    "  Global Superstore 2024         in_graph: false",
                    "  Superstore Datasource          in_graph: false",
                    "  … 9 more not in the graph, 1 already in it",
                ],
            ),
            (
                "tool",
                "add_sources",
                'pds_luids=[12 LUIDs], graph_id="sales"',
                ["status: running", "Ingestion runs in the background."],
            ),
            (
                "tool",
                "list_sources",
                'graph_id="sales"',
                ["count: 13", "ingest.status: completed", "  pds: 12 of 12 succeeded, 365 nodes, 460 edges"],
            ),
            (
                "assistant",
                "Done. All 13 Superstore data sources are now in the Sales graph: 12 added and "
                "ingested (365 nodes, 460 relationships), 1 was already there.",
            ),
        ],
    ),
    "demo-agent-check.gif": Scene(
        "Claude  ·  biztory-tableau-knowledge",
        [
            ("user", "Ask Tableau's agent how many data sources this graph has, and check its answer."),
            (
                "tool",
                "ask_tableau_agent",
                'question="How many published data sources are in this graph?"',
                [
                    'answer: "11',
                    "  - Global Superstore 2024",
                    "  - Sample - Superstore",
                    "  - Superstore Datasource",
                    '  … (13 names in total)"',
                    "tools_used: [search_sources]",
                ],
            ),
            ("tool", "list_sources", 'node_type="PDS"', ["count: 13"]),
            (
                "assistant",
                "The agent's list is right but its count isn't: it says 11 and then names 13, and the "
                "graph has 13. Worth adding to your answer-key test before people rely on counts.",
            ),
        ],
    ),
    "demo-document-context.gif": Scene(
        "Claude  ·  biztory-tableau-knowledge",
        [
            ("user", "Upload our KPI glossary as graph-wide business context."),
            (
                "tool",
                "create_semantic_context_from_document",
                'file_path="kpi-glossary.md", is_global=true, name="KPI glossary"',
                [
                    "id: semctx:…  kind: document",
                    "statements:",
                    "  - Profit Ratio is SUM(Profit) divided by SUM(Sales), as a %.",
                    "  - A late shipment ships more than 4 days after the order.",
                    "statements_skipped: []",
                ],
            ),
            (
                "assistant",
                "Added “KPI glossary” as graph-wide context with 2 statements, one per line of the file. "
                "Tableau's agent will now use these definitions when it answers.",
            ),
        ],
    ),
}

if __name__ == "__main__":
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, scene in SCENES.items():
        path = scene.render(out_dir / name)
        print(
            path, f"{path.stat().st_size / 1024:.0f} KB", f"{len(scene.frames)} frames", f"{W}x{scene.h // S}"
        )
