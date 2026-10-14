// Export the two editable WGO functional maps. No browser or package dependency.
import {readFileSync, writeFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';

const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;');

for (const locale of ['zh-CN', 'en']) {
  const source = new URL(`./semantic-functional.${locale}.excalidraw`, import.meta.url);
  const scene = JSON.parse(readFileSync(source, 'utf8'));
  const elements = scene.elements.filter(e => !e.isDeleted);
  const meta = elements.find(e => e.id === 'canvas').customData;
  const out = [`<svg xmlns="http://www.w3.org/2000/svg" width="${meta.width}" height="${meta.height}" viewBox="0 0 ${meta.width} ${meta.height}" role="img" aria-labelledby="title description" lang="${locale}">`,
    `<title id="title">${escape(meta.title)}</title><desc id="description">${escape(meta.description)}</desc>`,
    '<defs><marker id="arrow" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto" markerUnits="userSpaceOnUse"><path d="M0 0L10 4L0 8Z" fill="#216B5B"/></marker></defs>'];
  for (const e of elements.filter(e => e.type === 'rectangle')) {
    out.push(`<g id="${escape(e.id)}"><rect x="${e.x}" y="${e.y}" width="${e.width}" height="${e.height}" rx="${e.roundness ? 12 : 0}" fill="${e.backgroundColor}" stroke="${e.strokeColor}" stroke-width="${e.strokeWidth}"/></g>`);
  }
  for (const e of elements.filter(e => e.type === 'arrow' || e.type === 'line')) {
    const d = e.points.map((p, i) => `${i ? 'L' : 'M'}${e.x + p[0]} ${e.y + p[1]}`).join(' ');
    out.push(`<g id="${escape(e.id)}"><path d="${d}" fill="none" stroke="${e.strokeColor}" stroke-width="${e.strokeWidth}"${e.strokeStyle === 'dashed' ? ' stroke-dasharray="10 7"' : ''}${e.type === 'arrow' ? ' marker-end="url(#arrow)"' : ''}/></g>`);
  }
  for (const e of elements.filter(e => e.type === 'text')) {
    const backgrounds = elements.filter(r => r.type === 'rectangle' && e.x >= r.x && e.y >= r.y
      && e.x + e.width <= r.x + r.width && e.y + e.height <= r.y + r.height);
    backgrounds.sort((a,b) => a.width*a.height - b.width*b.height);
    const background = backgrounds[0]?.backgroundColor || elements.find(r => r.id === 'canvas').backgroundColor;
    out.push(`<g id="${escape(e.id)}" data-readability-background="${background}"><text x="${e.x}" y="${e.y + e.fontSize}" font-family="'Avenir Next','PingFang SC','Noto Sans CJK SC',Arial,sans-serif" font-size="${e.fontSize}" font-weight="${e.customData?.weight || 400}" fill="${e.strokeColor}" data-readability="${e.customData?.role || 'body'}">`);
    e.text.split('\n').forEach((line, i) => out.push(`<tspan x="${e.x}" dy="${i ? e.fontSize * e.lineHeight : 0}">${escape(line)}</tspan>`));
    out.push('</text></g>');
  }
  out.push('</svg>');
  const target = new URL(`../assets/architecture/semantic-functional.${locale}.svg`, import.meta.url);
  writeFileSync(target, out.join('\n') + '\n');
  console.log(fileURLToPath(target));
}
