"""Build Testpilot.epf from source using an installed 1C Designer and an infobase."""
import argparse
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--exe', required=True, help='Full path to 1cv8.exe/1cv8 (Designer)')
    parser.add_argument('--base', required=True, help='Existing file infobase used by Designer')
    parser.add_argument('--output', default=str(Path(__file__).with_name('Testpilot.epf')))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    output = Path(args.output).absolute()
    log = output.with_suffix('.build.log')
    startup = None
    if hasattr(subprocess, 'STARTUPINFO'):
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
    completed = subprocess.run([args.exe, 'DESIGNER', '/F', str(Path(args.base).absolute()),
        '/LoadExternalDataProcessorOrReportFromFiles', str(root / 'src/Testpilot.xml'), str(output),
        '/Out', str(log), '/DisableStartupDialogs', '/DisableStartupMessages'],
        startupinfo=startup, timeout=120)
    if completed.returncode or not output.is_file():
        raise SystemExit('Build failed; see ' + str(log))
    print(output)


if __name__ == '__main__':
    main()
