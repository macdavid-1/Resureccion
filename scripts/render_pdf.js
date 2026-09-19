#!/usr/bin/env node
/**
 * Resurrección PDF export sidecar.
 *
 * Reads a JSON job on stdin, renders a premium 6×9in PDF with PDFKit, and
 * writes it to the requested output path. The document design lives here;
 * the TEXTUAL content arrives as pre-structured blocks and is rendered
 * word for word — never summarized, rewritten or truncated.
 *
 * Job shape:
 * {
 *   "out": "/path/to/report.pdf",
 *   "title": "...", "subtitle": "...",
 *   "footer": "left  |  right",
 *   "blocks": [ {"t":"h1|h2|h3|p|li|oli|quote|hr|table|img|kv|chip", ...} ]
 * }
 *
 * Block details:
 *   h1/h2/h3/p : {"t":..., "x":"text"}
 *   li/oli     : {"t":..., "x":"text"}            (bulleted / numbered)
 *   quote      : {"t":"quote", "x":"text"}
 *   hr         : {"t":"hr"}
 *   chip       : {"t":"chip", "x":"text"}          (small caps status pill)
 *   table      : {"t":"table", "head":["c1",...], "rows":[["c1",...],...]}
 *   kv         : {"t":"kv", "head":["k","v"], "rows":[["k","v"],...]}  (2-col key/value)
 *   img        : {"t":"img", "path":"/abs/file.png", "caption":"..."}   (optional)
 *
 * Exit codes: 0 ok, 3 bad job, 4 render error.
 */
const fs = require("fs");
const path = require("path");
const PDFDocument = require("pdfkit");

const PAGE_W = 6 * 72;   // 6 inches
const PAGE_H = 9 * 72;   // 9 inches
const MARGIN = 0.72 * 72;
const ACCENT = "#1207DA";
const INK = "#111318";
const INK_2 = "#4b5162";
const LINE = "#d8dce8";

const SERIF = "Times-Roman";
const SERIF_BOLD = "Times-Bold";
const SERIF_ITAL = "Times-Italic";
const SANS = "Helvetica";
const SANS_BOLD = "Helvetica-Bold";

function fail(code, msg) {
  process.stderr.write(`[render_pdf] ${msg}\n`);
  process.exit(code);
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let buf = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (d) => (buf += d));
    process.stdin.on("end", () => resolve(buf));
    process.stdin.on("error", reject);
  });
}

/** Sanitize to plain text: the content is owner-facing prose; we only strip
 * markdown emphasis/backticks so PDFKit renders clean type. Words are kept. */
function plain(x) {
  return String(x == null ? "" : x)
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1 ($2)")
    .trim();
}

class Renderer {
  constructor(doc, job) {
    this.doc = doc;
    this.job = job;
    this.pageNo = 1;
    this.y = MARGIN;
  }

  contentBottom() {
    return PAGE_H - MARGIN - 26; // room for footer
  }

  ensure(h) {
    if (this.y + h > this.contentBottom()) {
      this.doc.addPage({ size: [PAGE_W, PAGE_H], margin: 0 });
      this.pageNo++;
      this.y = MARGIN;
    }
  }

  footer() {
    const d = this.doc;
    const y = PAGE_H - 40;
    d.font(SANS).fontSize(7).fillColor(INK_2);
    d.text(this.job.footer || "", MARGIN, y, { width: PAGE_W - 2 * MARGIN, lineBreak: false });
    const n = String(this.pageNo).padStart(2, "0");
    d.text(n, MARGIN, y, { width: PAGE_W - 2 * MARGIN, align: "right", lineBreak: false });
    d.moveTo(MARGIN, y - 8).lineTo(PAGE_W - MARGIN, y - 8).lineWidth(0.5).strokeColor(LINE).stroke();
  }

  gap(h) {
    this.y += h;
  }

  rule(color = LINE, w = 0.6) {
    this.doc.moveTo(MARGIN, this.y).lineTo(PAGE_W - MARGIN, this.y)
      .lineWidth(w).strokeColor(color).stroke();
  }

  text(x, opts) {
    const d = this.doc;
    const o = Object.assign({ width: PAGE_W - 2 * MARGIN }, opts);
    this.ensure(14);
    const before = this.y;
    d.text(x, MARGIN, this.y, o);
    this.y = d.y + (o.after || 0);
    return this.y - before;
  }

  heading(text, level) {
    const d = this.doc;
    this.gap(level === 1 ? 14 : 12);
    this.ensure(level === 1 ? 40 : 32);
    if (level === 2) {
      this.ensure(34);
      this.y += 4;
      d.font(SANS_BOLD).fontSize(9.5).fillColor(ACCENT);
      d.text(text.toUpperCase(), MARGIN, this.y, { width: PAGE_W - 2 * MARGIN, characterSpacing: 0.8 });
      this.y = d.y + 3;
      this.rule(ACCENT, 0.7);
      this.y += 10;
      return;
    }
    if (level === 1) {
      d.font(SANS_BOLD).fontSize(15).fillColor(INK);
      d.text(text, MARGIN, this.y, { width: PAGE_W - 2 * MARGIN });
      this.y = d.y + 8;
      return;
    }
    d.font(SANS_BOLD).fontSize(10.5).fillColor(INK);
    d.text(text, MARGIN, this.y, { width: PAGE_W - 2 * MARGIN });
    this.y = d.y + 6;
  }

  para(text) {
    this.doc.font(SERIF).fontSize(9.8).fillColor(INK);
    this.text(text, { align: "justify", lineGap: 2.4, after: 7 });
  }

  bullet(text, i, ordered) {
    const d = this.doc;
    const marker = ordered ? `${i}.` : "–";
    this.ensure(16);
    d.font(SERIF).fontSize(9.6).fillColor(INK);
    const h = d.heightOfString(text, { width: PAGE_W - 2 * MARGIN - 16 });
    if (this.y + h > this.contentBottom()) {
      d.addPage({ size: [PAGE_W, PAGE_H], margin: 0 });
      this.pageNo++;
      this.y = MARGIN;
    }
    d.font(SANS).fontSize(8.6).fillColor(ACCENT)
      .text(marker, MARGIN, this.y + 1.5, { width: 14, lineBreak: false });
    d.font(SERIF).fontSize(9.6).fillColor(INK)
      .text(text, MARGIN + 16, this.y, { width: PAGE_W - 2 * MARGIN - 16, lineGap: 1.8 });
    this.y += Math.max(h, 13) + 3.5;
  }

  quote(text) {
    const d = this.doc;
    this.ensure(30);
    const h = d.heightOfString(text, { width: PAGE_W - 2 * MARGIN - 22 });
    if (this.y + h + 10 > this.contentBottom()) {
      d.addPage({ size: [PAGE_W, PAGE_H], margin: 0 });
      this.pageNo++;
      this.y = MARGIN;
    }
    d.moveTo(MARGIN + 2, this.y).lineTo(MARGIN + 2, this.y + h + 6)
      .lineWidth(2).strokeColor(ACCENT).stroke();
    d.font(SERIF_ITAL).fontSize(9.6).fillColor(INK_2)
      .text(text, MARGIN + 14, this.y + 2, { width: PAGE_W - 2 * MARGIN - 22, lineGap: 2 });
    this.y += h + 14;
  }

  chip(text) {
    const d = this.doc;
    d.font(SANS_BOLD).fontSize(7.5);
    const w = d.widthOfString(text) + 14;
    this.ensure(22);
    d.roundedRect(MARGIN, this.y, w, 15, 7.5).fillAndStroke("#eef0ff", ACCENT);
    d.fillColor(ACCENT).text(text, MARGIN + 7, this.y + 4, { lineBreak: false });
    this.y += 21;
  }

  table(head, rows) {
    const d = this.doc;
    const cols = head.length;
    if (!cols) return;
    const totalW = PAGE_W - 2 * MARGIN;
    // Column widths: measure max content, distribute with min width.
    d.font(SANS).fontSize(7.6);
    const weights = head.map((h, i) => {
      let m = d.widthOfString(plain(h)) + 10;
      for (const r of rows.slice(0, 40)) {
        const w = d.widthOfString(plain(r[i] || ""));
        if (w > m) m = w + 10;
      }
      return m;
    });
    const sum = weights.reduce((a, b) => a + b, 0) || 1;
    const widths = weights.map((w) => Math.max(40, (w / sum) * totalW));
    const scale = totalW / widths.reduce((a, b) => a + b, 0);
    const W = widths.map((w) => w * scale);

    const rowH = (cells) => {
      let h = 12;
      d.font(SERIF).fontSize(7.8);
      cells.forEach((c, i) => {
        const hh = d.heightOfString(plain(c), { width: W[i] - 8 });
        if (hh > h) h = hh;
      });
      return h + 7;
    };

    // Header
    const headH = 18;
    this.ensure(headH + 24);
    d.rect(MARGIN, this.y, totalW, headH).fill("#f0f1fa");
    d.font(SANS_BOLD).fontSize(7.2).fillColor(INK_2);
    let x = MARGIN;
    head.forEach((h, i) => {
      d.text(plain(h).toUpperCase(), x + 4, this.y + 5.5, { width: W[i] - 8, characterSpacing: 0.5, lineBreak: false });
      x += W[i];
    });
    this.y += headH;
    d.moveTo(MARGIN, this.y).lineTo(PAGE_W - MARGIN, this.y).lineWidth(0.7).strokeColor(ACCENT).stroke();
    this.y += 1;

    // Rows (row-wise page breaks; never split a row)
    for (const r of rows) {
      const h = rowH(r);
      if (this.y + h > this.contentBottom()) {
        d.addPage({ size: [PAGE_W, PAGE_H], margin: 0 });
        this.pageNo++;
        this.y = MARGIN;
        d.rect(MARGIN, this.y, totalW, headH).fill("#f0f1fa");
        d.font(SANS_BOLD).fontSize(7.2).fillColor(INK_2);
        let xx = MARGIN;
        head.forEach((hh, i) => {
          d.text(plain(hh).toUpperCase(), xx + 4, this.y + 5.5, { width: W[i] - 8, characterSpacing: 0.5, lineBreak: false });
          xx += W[i];
        });
        this.y += headH;
        d.moveTo(MARGIN, this.y).lineTo(PAGE_W - MARGIN, this.y).lineWidth(0.7).strokeColor(ACCENT).stroke();
        this.y += 1;
      }
      d.font(SERIF).fontSize(7.8).fillColor(INK);
      let xx = MARGIN;
      r.forEach((c, i) => {
        d.text(plain(c), xx + 4, this.y + 3.5, { width: W[i] - 8, lineGap: 0.5 });
        xx += W[i];
      });
      this.y += h;
      d.moveTo(MARGIN, this.y).lineTo(PAGE_W - MARGIN, this.y).lineWidth(0.4).strokeColor(LINE).stroke();
    }
    this.y += 10;
  }

  image(block) {
    const d = this.doc;
    try {
      if (!block.path || !fs.existsSync(block.path)) return;
      const maxW = PAGE_W - 2 * MARGIN;
      const probe = d.openImage(block.path);
      const scale = Math.min(1, maxW / probe.width);
      const w = probe.width * scale;
      const h = probe.height * scale;
      this.ensure(h + 18);
      d.image(probe, MARGIN, this.y, { width: w });
      this.y += h + 4;
      if (block.caption) {
        d.font(SANS).fontSize(7).fillColor(INK_2)
          .text(plain(block.caption), MARGIN, this.y, { width: maxW });
        this.y = d.y + 6;
      }
    } catch (e) {
      // A missing/broken image must never abort the whole export.
      this.para(`[screenshot unavailable: ${plain(block.caption || block.path)}]`);
    }
  }

  kv(head, rows) {
    // Two-column key/value table (used for opportunity dossiers).
    this.table(head && head.length === 2 ? head : ["Field", "Value"], rows);
  }

  cover() {
    const d = this.doc;
    d.addPage({ size: [PAGE_W, PAGE_H], margin: 0 });
    // Premium dark cover page.
    d.rect(0, 0, PAGE_W, PAGE_H).fill("#000000");
    d.rect(0, PAGE_H * 0.42, PAGE_W, 2.2).fill(ACCENT);
    d.font(SANS).fontSize(7.5).fillColor("#5d6890");
    d.text("R E S U R R E C C I Ó N", MARGIN, PAGE_H * 0.30, { characterSpacing: 3 });
    d.font(SANS_BOLD).fontSize(19).fillColor("#f2f4fa");
    d.text(this.job.title || "Market Intelligence Report", MARGIN, PAGE_H * 0.46, {
      width: PAGE_W - 2 * MARGIN, lineGap: 3,
    });
    if (this.job.subtitle) {
      d.font(SERIF_ITAL).fontSize(10).fillColor("#aab3d0");
      d.text(this.job.subtitle, MARGIN, d.y + 10, { width: PAGE_W - 2 * MARGIN, lineGap: 2 });
    }
    d.font(SANS).fontSize(7).fillColor("#5d6890");
    d.text(this.job.footer || "", MARGIN, PAGE_H - 58, { width: PAGE_W - 2 * MARGIN, lineGap: 2 });
    d.addPage({ size: [PAGE_W, PAGE_H], margin: 0 });
    this.pageNo = 2;
    this.y = MARGIN;
  }

  run(blocks) {
    this.cover();
    let orderedIdx = 0;
    for (const b of blocks || []) {
      if (!b || !b.t) continue;
      orderedIdx = b.t === "oli" ? orderedIdx + 1 : 0;
      switch (b.t) {
        case "h1": this.heading(plain(b.x), 1); break;
        case "h2": this.heading(plain(b.x), 2); break;
        case "h3": this.heading(plain(b.x), 3); break;
        case "p": {
          const t = plain(b.x);
          if (t) this.para(t);
          break;
        }
        case "li": this.bullet(plain(b.x), 0, false); break;
        case "oli": this.bullet(plain(b.x), orderedIdx, true); break;
        case "quote": this.quote(plain(b.x)); break;
        case "hr": this.gap(6); this.rule(); this.gap(10); break;
        case "chip": this.chip(plain(b.x)); break;
        case "table": this.table(b.head || [], b.rows || []); break;
        case "kv": this.kv(b.head, b.rows || []); break;
        case "img": this.image(b); break;
        default: break;
      }
    }
    // Footer every page except the cover.
    const range = this.doc.bufferedPageRange();
    for (let i = range.start + 1; i < range.start + range.count; i++) {
      this.doc.switchToPage(i);
      this.footer();
    }
  }
}

async function main() {
  let job;
  try {
    job = JSON.parse(await readStdin());
  } catch (e) {
    return fail(3, `invalid job JSON: ${e.message}`);
  }
  if (!job || !job.out || !Array.isArray(job.blocks)) {
    return fail(3, "job requires 'out' and 'blocks'");
  }
  try {
    const doc = new PDFDocument({
      size: [PAGE_W, PAGE_H],
      margin: 0,
      bufferPages: true, // footer pass needs switchToPage
      autoFirstPage: false,
      info: {
        Title: job.title || "Resurrección Report",
        Author: "Resurrección",
        Creator: "Resurrección PDF Export",
      },
    });
    const fileStream = fs.createWriteStream(job.out);
    doc.pipe(fileStream);
    new Renderer(doc, job).run(job.blocks);
    doc.end();
    await new Promise((resolve, reject) => {
      // The FILE stream must finish flushing, not just the document.
      const timer = setTimeout(() => reject(new Error("pdf render timeout")), 120000);
      const done = (fn) => (v) => { clearTimeout(timer); fn(v); };
      fileStream.on("finish", done(resolve));
      fileStream.on("error", done(reject));
      doc.on("error", done(reject));
    });
    const size = fs.statSync(job.out).size;
    if (size < 500) return fail(4, `suspiciously small PDF (${size} bytes)`);
    process.stdout.write(JSON.stringify({ ok: true, bytes: size }));
  } catch (e) {
    return fail(4, `render failed: ${e.message}`);
  }
}

main();
