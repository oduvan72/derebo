const fs = require('node:fs');
const path = require('node:path');

const packages = [
  '@fontsource-variable/caveat',
  '@fontsource-variable/manrope',
  '@fontsource-variable/space-grotesk',
];
const destination = path.resolve(__dirname, '../static/files');

fs.mkdirSync(destination, { recursive: true });

for (const packageName of packages) {
  const source = path.resolve(__dirname, '../node_modules', packageName, 'files');
  const fontFiles = fs.readdirSync(source).filter((file) => file.endsWith('.woff2'));

  if (fontFiles.length === 0) {
    throw new Error(`No WOFF2 font files found in ${packageName}`);
  }

  for (const file of fontFiles) {
    fs.copyFileSync(path.join(source, file), path.join(destination, file));
  }
}
