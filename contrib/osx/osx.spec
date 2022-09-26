# -*- mode: python -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

import sys, os

PACKAGE='Electrum-FIRO'
PYPKG='electrum_firo'
MAIN_SCRIPT='electrum-firo'
ICONS_FILE=PYPKG + '/gui/icons/electrum-firo.icns'


for i, x in enumerate(sys.argv):
    if x == '--name':
        VERSION = sys.argv[i+1]
        break
else:
    raise Exception('no version')

electrum = os.path.abspath(".") + "/"
block_cipher = None

# see https://github.com/pyinstaller/pyinstaller/issues/2005
hiddenimports = []
hiddenimports += collect_submodules('pkg_resources')  # workaround for https://github.com/pypa/setuptools/issues/1963
hiddenimports += collect_submodules('trezorlib')
hiddenimports += collect_submodules('safetlib')
hiddenimports += collect_submodules('btchip')
hiddenimports += collect_submodules('keepkeylib')
hiddenimports += collect_submodules('websocket')
hiddenimports += ['PyQt5.QtPrintSupport']  # needed by Revealer

datas = [
    (electrum-firo + PYPKG + '/*.json', PYPKG),
    (electrum-firo + PYPKG + '/lnwire/*.csv', PYPKG + '/lnwire'),
    (electrum-firo + PYPKG + '/wordlist/english.txt', PYPKG + '/wordlist'),
    (electrum-firo + PYPKG + '/wordlist/slip39.txt', PYPKG + '/wordlist'),
    (electrum-firo + PYPKG + '/locale', PYPKG + '/locale'),
    (electrum-firo + PYPKG + '/plugins', PYPKG + '/plugins'),
    (electrum-firo + PYPKG + '/gui/icons', PYPKG + '/gui/icons'),
]
datas += collect_data_files('trezorlib')
datas += collect_data_files('safetlib')
datas += collect_data_files('btchip')
datas += collect_data_files('keepkeylib')

# Add libusb so Trezor and Safe-T mini will work
binaries = [(electrum-firo + "contrib/osx/libusb-1.0.dylib", ".")]
binaries += [(electrum-firo + "contrib/osx/libsecp256k1.0.dylib", ".")]
binaries += [(electrum + "contrib/osx/libzbar.0.dylib", ".")]

# Workaround for "Retro Look":
binaries += [b for b in collect_dynamic_libs('PyQt5') if 'macstyle' in b[0]]

# We don't put these files in to actually include them in the script but to make the Analysis method scan them for imports
a = Analysis([electrum-firo+ MAIN_SCRIPT,
              electrum-firo+'electrum_firo/gui/qt/main_window.py',
              electrum-firo+'electrum_firo/gui/qt/qrreader/qtmultimedia/camera_dialog.py',
              electrum-firo+'electrum_firo/gui/text.py',
              electrum-firo+'electrum_firo/util.py',
              electrum-firo+'electrum_firo/wallet.py',
              electrum-firo+'electrum/simple_config.py',
              electrum-firo+'electrum_firo/bitcoin.py',
              electrum-firo+'electrum_firo/dnssec.py',
              electrum-firo+'electrum_firo/commands.py',
              electrum-firo+'electrum_firo/plugins/cosigner_pool/qt.py',
              electrum-firo+'electrum_firo/plugins/trezor/qt.py',
              electrum-firo+'electrum_firo/plugins/safe_t/client.py',
              electrum-firo+'electrum_firo/plugins/safe_t/qt.py',
              electrum-firo+'electrum_firo/plugins/keepkey/qt.py',
              electrum-firo+'electrum_firo/plugins/ledger/qt.py',
              electrum-firo+'electrum_firo/plugins/coldcard/qt.py',
              ],
             binaries=binaries,
             datas=datas,
             hiddenimports=hiddenimports,
             hookspath=[])

# http://stackoverflow.com/questions/19055089/pyinstaller-onefile-warning-pyconfig-h-when-importing-scipy-or-scipy-signal
for d in a.datas:
    if 'pyconfig' in d[0]:
        a.datas.remove(d)
        break

# Strip out parts of Qt that we never use. Reduces binary size by tens of MBs. see #4815
qt_bins2remove=('qtweb', 'qt3d', 'qtgame', 'qtdesigner', 'qtquick', 'qtlocation', 'qttest', 'qtxml')
print("Removing Qt binaries:", *qt_bins2remove)
for x in a.binaries.copy():
    for r in qt_bins2remove:
        if x[0].lower().startswith(r):
            a.binaries.remove(x)
            print('----> Removed x =', x)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name=MAIN_SCRIPT,
    debug=False,
    strip=False,
    upx=True,
    icon=electrum+ICONS_FILE,
    console=False,
)

app = BUNDLE(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    version = VERSION,
    name=PACKAGE + '.app',
    icon=electrum+ICONS_FILE,
    bundle_identifier=None,
    info_plist={
        'NSHighResolutionCapable': 'True',
        'NSSupportsAutomaticGraphicsSwitching': 'True'
    },
)
