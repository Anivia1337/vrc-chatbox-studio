pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean -wF --name "VRC-Chatbox-Studio" --icon ui\icon.ico --add-data "ui;ui" studio.py
