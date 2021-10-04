# -*- mode: python -*-
import os
import os.path
import sys
from PyInstaller.utils.hooks import (collect_data_files, collect_submodules,
                                     collect_dynamic_libs)


for i, x in enumerate(sys.argv):
    if x == '--name':
        cmdline_name = sys.argv[i+1]
        break
else:
    raise Exception('no name')

TRAVIS_TAG = os.environ.get('TRAVIS_TAG')
DASH_ELECTRUM_VERSION = os.environ.get('DASH_ELECTRUM_VERSION')
ICONS_FILE = 'electrum_firo/gui/icons/electrum-firo.icns'

hiddenimports = []
hiddenimports += collect_submodules('pkg_resources')  # workaround for https://github.com/pypa/setuptools/issues/1963
hiddenimports += collect_submodules('trezorlib')
hiddenimports += collect_submodules('safetlib')
hiddenimports += collect_submodules('btchip')
hiddenimports += collect_submodules('keepkeylib')
hiddenimports += collect_submodules('websocket')

# safetlib imports PyQt5.Qt.  We use a local updated copy of pinmatrix.py until they
# release a new version that includes https://github.com/archos-safe-t/python-safet/commit/b1eab3dba4c04fdfc1fcf17b66662c28c5f2380e
hiddenimports.remove('safetlib.qt.pinmatrix')

hiddenimports += [
    'electrum_firo',
    'electrum_firo.base_crash_reporter',
    'electrum_firo.base_wizard',
    'electrum_firo.plot',
    'electrum_firo.qrscanner',
    'electrum_firo.websockets',
    'electrum_firo.gui.qt',
    'electrum_firo.gui.qt.qrreader.qtmultimedia.camera_dialog',
    'PyQt5.sip',
    'PyQt5.QtPrintSupport',  # needed by Revealer

    'electrum_firo.plugins',

    'electrum_firo.plugins.hw_wallet.qt',

    'electrum_firo.plugins.audio_modem.qt',
    'electrum_firo.plugins.cosigner_pool.qt',
    'electrum_firo.plugins.digitalbitbox.qt',
    'electrum_firo.plugins.email_requests.qt',
    'electrum_firo.plugins.keepkey.qt',
    'electrum_firo.plugins.revealer.qt',
    'electrum_firo.plugins.labels.qt',
    'electrum_firo.plugins.scan_over_gap.qt',
    'electrum_firo.plugins.trezor.qt',
    'electrum_firo.plugins.safe_t.client',
    'electrum_firo.plugins.safe_t.qt',
    'electrum_firo.plugins.ledger.qt',
    'electrum_firo.plugins.virtualkeyboard.qt',
]

datas = [
    ('electrum_firo/checkpoints*.*', 'electrum_firo'),
    ('electrum_firo/*.json', 'electrum_firo'),
    ('electrum_firo/locale', 'electrum_firo/locale'),
    ('electrum_firo/wordlist', 'electrum_firo/wordlist'),
    ('electrum_firo/gui/icons', 'electrum_firo/gui/icons'),
]

datas += collect_data_files('trezorlib')
datas += collect_data_files('safetlib')
datas += collect_data_files('btchip')
datas += collect_data_files('keepkeylib')

# Add libusb so Trezor and Safe-T mini will work
binaries = []
binaries += [('../libusb-1.0.dylib', '.')]
binaries += [('../libsecp256k1.0.dylib', '.')]
binaries += [('../libzbar.0.dylib', '.')]

# Workaround for "Retro Look":
binaries += [b for b in collect_dynamic_libs('PyQt5') if 'macstyle' in b[0]]

# https://github.com/pyinstaller/pyinstaller/wiki/Recipe-remove-tkinter-tcl
sys.modules['FixTk'] = None
excludes = ['FixTk', 'tcl', 'tk', '_tkinter', 'tkinter', 'Tkinter']
excludes += [
    'PyQt5.QtBluetooth',
    'PyQt5.QtCLucene',
    'PyQt5.QtDBus',
    'PyQt5.Qt5CLucene',
    'PyQt5.QtDesigner',
    'PyQt5.QtDesignerComponents',
    'PyQt5.QtHelp',
    'PyQt5.QtLocation',
    'PyQt5.QtMultimediaQuick_p',
    'PyQt5.QtMultimediaWidgets',
    'PyQt5.QtNetworkAuth',
    'PyQt5.QtNfc',
    'PyQt5.QtOpenGL',
    'PyQt5.QtPositioning',
    'PyQt5.QtQml',
    'PyQt5.QtQuick',
    'PyQt5.QtQuickParticles',
    'PyQt5.QtQuickWidgets',
    'PyQt5.QtSensors',
    'PyQt5.QtSerialPort',
    'PyQt5.QtSql',
    'PyQt5.Qt5Sql',
    'PyQt5.Qt5Svg',
    'PyQt5.QtTest',
    'PyQt5.QtWebChannel',
    'PyQt5.QtWebEngine',
    'PyQt5.QtWebEngineCore',
    'PyQt5.QtWebEngineWidgets',
    'PyQt5.QtWebKit',
    'PyQt5.QtWebKitWidgets',
    'PyQt5.QtWebSockets',
    'PyQt5.QtXml',
    'PyQt5.QtXmlPatterns',
    'PyQt5.QtWebProcess',
    'PyQt5.QtWinExtras',
]

a = Analysis(['electrum-firo'],
             hiddenimports=hiddenimports,
             datas=datas,
             binaries=binaries,
             excludes=excludes,
             runtime_hooks=['pyi_runtimehook.py'])

# http://stackoverflow.com/questions/19055089/
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

pyz = PYZ(a.pure)

exe = EXE(pyz,
          a.scripts,
          exclude_binaries=True,
          debug=False,
          strip=False,
          upx=False,
          console=False,
          icon='electrum_firo/gui/icons/electrum-firo.ico',
          name=os.path.join('build/electrum-dash/electrum-firo', cmdline_name))

coll = COLLECT(exe,
               a.binaries,
               a.datas,
               strip=False,
               upx=False,
               name=os.path.join('dist', 'electrum-dash'))

app = BUNDLE(coll,
             info_plist={
                'NSHighResolutionCapable': True,
                'NSSupportsAutomaticGraphicsSwitching': True,
                'CFBundleURLTypes': [
                    {'CFBundleURLName': 'dash', 'CFBundleURLSchemes': ['dash']}
                ],
             },
             name=os.path.join('dist', 'Firo Electrum.app'),
             appname="Firo Electrum",
	         icon=ICONS_FILE,
             version=DASH_ELECTRUM_VERSION)
