//bun-extra-requirements:
//playwright@1.48.0
//chromium-bidi@0.8.0

// Render an explainer letter (full HTML string) to Letter-size PDF bytes.
// Runs on the chromium-tagged pool (the ION scrapers' workers). The page's
// own @media print rules apply: zero page margins, print bar hidden. Fonts
// load from Google Fonts, hence networkidle.
import { chromium } from "playwright@1.40.0";

export async function main(html: string) {
  const browser = await chromium.launch({
    executablePath: "/usr/bin/chromium",
    args: ["--no-sandbox", "--single-process", "--no-zygote", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
  });
  try {
    const page = await (await browser.newContext()).newPage();
    await page.setContent(html, { waitUntil: "networkidle", timeout: 45000 });
    const pdf = await page.pdf({ format: "Letter", printBackground: true });
    return { pdf_b64: Buffer.from(pdf).toString("base64"), bytes: pdf.length };
  } finally {
    await browser.close();
  }
}
