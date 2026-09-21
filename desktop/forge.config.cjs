const path = require('node:path');

const backendDirectory = path.resolve(
  __dirname,
  '..',
  'local-artifacts',
  'windows-package',
  'dist',
  'backend',
);
const iconPath = path.resolve(
  __dirname,
  '..',
  'src',
  'literature_evidence_mcp',
  'static',
  'icon.png',
);
const licensePath = path.resolve(__dirname, '..', 'LICENSE');

module.exports = {
  packagerConfig: {
    asar: true,
    executableName: 'FolioHook',
    extraResource: [backendDirectory, iconPath, licensePath],
  },
  makers: [
    {
      name: '@electron-forge/maker-squirrel',
      platforms: ['win32'],
      config: {
        name: 'FolioHook',
        setupExe: 'FolioHook Setup.exe',
        noMsi: true,
      },
    },
  ],
};
