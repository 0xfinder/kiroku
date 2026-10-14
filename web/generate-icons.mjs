// Run from web/: npm run icons. Sharp rasterizes the repository's vector source.
import sharp from 'sharp';
import { readFile, mkdir } from 'node:fs/promises';
const source = await readFile('../assets/icon.svg');
await mkdir('static/icons', { recursive: true });
for (const [name, size] of [
  ['icon-192', 192],
  ['icon-512', 512],
  ['apple-touch-icon', 180],
]) {
  await sharp(source, { density: 600 })
    .resize(size, size)
    .png()
    .toFile(`static/icons/${name}.png`);
}
// The entire mark fits within the central 80% safe zone for maskable icons.
const mark = await sharp(source, { density: 600 })
  .resize(320, 320)
  .png()
  .toBuffer();
await sharp({
  create: { width: 512, height: 512, channels: 4, background: '#ea580c' },
})
  .composite([{ input: mark, left: 96, top: 96 }])
  .png()
  .toFile('static/icons/maskable-512.png');
