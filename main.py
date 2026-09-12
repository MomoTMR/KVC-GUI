import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime

from PyQt6.QtCore import QProcess, QSettings, QTimer, QLoggingCategory, QLockFile
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QComboBox, QGroupBox,
    QSystemTrayIcon, QMenu, QStatusBar, QMessageBox, QTabWidget, QInputDialog
)

CONFIG_PATH = "/etc/kerio-kvc.conf"


class KerioKvcGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.lock_file = None
        self.setWindowTitle("Kerio Control VPN Client GUI")
        self.setWindowIcon(QIcon.fromTheme("network-vpn"))
        self.resize(720, 580)

        # Хранилище профилей (profiles.ini в папке проекта)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.profiles_path = os.path.join(base_dir, "profiles.ini")
        self.settings = QSettings(self.profiles_path, QSettings.Format.IniFormat)

        # Процесс для чтения логов journalctl
        self.log_process = QProcess(self)
        self.log_process.readyReadStandardOutput.connect(self.handle_log_output)

        # Таймер проверки статуса службы
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(3000)
        self.status_timer.timeout.connect(self.check_service_status)

        self.init_ui()
        self.init_tray()

        # Загрузка списка профилей и автоматический выбор первого при старте
        self.load_profiles_to_combo()
        self.auto_load_first_profile()

        self.status_timer.start()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # --- 1. Параметры подключения ---
        auth_group = QGroupBox("Параметры Kerio VPN")
        auth_layout = QVBoxLayout()

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Сервер:"))
        self.server_input = QLineEdit()
        self.server_input.setPlaceholderText("vpn.company.com или IP")
        row1.addWidget(self.server_input)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Логин:"))
        self.login_input = QLineEdit()
        row2.addWidget(self.login_input)

        row2.addWidget(QLabel("Пароль:"))
        self.pass_input = QLineEdit()
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        row2.addWidget(self.pass_input)

        auth_layout.addLayout(row1)
        auth_layout.addLayout(row2)
        auth_group.setLayout(auth_layout)
        main_layout.addWidget(auth_group)

        # --- 2. Управление профилями и службой ---
        tabs = QTabWidget()

        control_tab = QWidget()
        ctrl_layout = QVBoxLayout(control_tab)

        # Профили
        profile_layout = QHBoxLayout()
        profile_layout.addWidget(QLabel("Профиль:"))
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self.on_profile_selected)
        profile_layout.addWidget(self.profile_combo, 1)

        self.btn_save_profile = QPushButton("Сохранить")
        self.btn_save_profile.clicked.connect(self.save_profile)
        self.btn_delete_profile = QPushButton("Удалить")
        self.btn_delete_profile.clicked.connect(self.delete_profile)

        profile_layout.addWidget(self.btn_save_profile)
        profile_layout.addWidget(self.btn_delete_profile)
        ctrl_layout.addLayout(profile_layout)

        # Кнопки управления службой
        action_layout = QHBoxLayout()
        self.btn_start = QPushButton("Старт (Start)")
        self.btn_start.clicked.connect(self.start_service)

        self.btn_stop = QPushButton("Стоп (Stop)")
        self.btn_stop.clicked.connect(self.stop_service)

        self.btn_restart = QPushButton("Перезапуск (Restart)")
        self.btn_restart.clicked.connect(self.restart_service)

        action_layout.addWidget(self.btn_start)
        action_layout.addWidget(self.btn_stop)
        action_layout.addWidget(self.btn_restart)
        ctrl_layout.addLayout(action_layout)

        tabs.addTab(control_tab, "Управление")
        main_layout.addWidget(tabs)

        # --- 3. Логи службы ---
        log_group = QGroupBox("Журнал службы kerio-kvc")
        log_layout = QVBoxLayout()

        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setFont(QFont("Monospace", 9))
        self.log_area.setStyleSheet("background-color: #1e1e1e; color: #00ff00;")

        btn_log_layout = QHBoxLayout()
        btn_fetch_log = QPushButton("Обновить логи")
        btn_fetch_log.clicked.connect(self.fetch_logs)
        btn_clear_log = QPushButton("Очистить")
        btn_clear_log.clicked.connect(self.log_area.clear)

        btn_log_layout.addWidget(btn_fetch_log)
        btn_log_layout.addStretch()
        btn_log_layout.addWidget(btn_clear_log)

        log_layout.addWidget(self.log_area)
        log_layout.addLayout(btn_log_layout)
        log_group.setLayout(log_layout)
        main_layout.addWidget(log_group)

        # --- 4. Строка состояния ---
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("Проверка статуса...")
        self.status_bar.addPermanentWidget(self.status_label)

    # --- Резервное копирование и запись конфигурации ---

    def backup_update_and_run_systemctl(self, action, server, login, password):
        """Выполняет обновление конфигурации, бэкап и запуск/перезапуск службы за 1 вызов pkexec."""
        # Код вспомогательного Python-скрипта, который будет запущен от root
        helper_code = f"""import os
import sys
import glob
import xml.etree.ElementTree as ET
from datetime import datetime
import subprocess

CONFIG_PATH = "/etc/kerio-kvc.conf"

def main():
    if len(sys.argv) < 5:
        print("[ОШИБКА] Недостаточно аргументов", file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1]
    server = sys.argv[2]
    login = sys.argv[3]
    password = sys.argv[4]

    # 1. Чтение существующего конфига
    if not os.path.exists(CONFIG_PATH):
        print(f"[ОШИБКА] Конфиг {{CONFIG_PATH}} не найден", file=sys.stderr)
        sys.exit(1)

    try:
        tree = ET.parse(CONFIG_PATH)
    except Exception as e:
        print(f"[ОШИБКА] Ошибка парсинга XML: {{e}}", file=sys.stderr)
        sys.exit(1)

    # 2. Модификация XML
    server_elem = tree.find(".//server")
    if server_elem is not None:
        server_elem.text = server
    
    username_elem = tree.find(".//username")
    if username_elem is not None:
        username_elem.text = login
        
    password_elem = tree.find(".//password")
    if password_elem is not None:
        password_elem.text = password

    mapping = {{"Server": server, "Username": login, "Password": password}}
    for elem in tree.getroot().iter("variable"):
        var_name = elem.attrib.get("name")
        if var_name in mapping and mapping[var_name] is not None:
            elem.text = mapping[var_name]

    # 3. Бэкап и ротация (оставляем последние 10)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{{CONFIG_PATH}}.bak_{{timestamp}}"
    try:
        import shutil
        shutil.copy2(CONFIG_PATH, backup_path)
        
        backups = sorted(glob.glob(f"{{CONFIG_PATH}}.bak_*"), key=os.path.getmtime)
        if len(backups) > 10:
            for b in backups[:-10]:
                os.remove(b)
        print(f"Бэкап успешно создан: {{backup_path}}")
    except Exception as e:
        print(f"[ПРЕДУПРЕЖДЕНИЕ] Не удалось создать бэкап: {{e}}", file=sys.stderr)

    # 4. Запись нового конфига
    try:
        tree.write(CONFIG_PATH, encoding="utf-8", xml_declaration=True)
        print("Конфигурация успешно обновлена.")
    except Exception as e:
        print(f"[ОШИБКА] Не удалось записать XML: {{e}}", file=sys.stderr)
        sys.exit(1)

    # 5. Управление службой через systemctl
    try:
        print(f"Выполнение: systemctl {{action}} kerio-kvc")
        res = subprocess.run(["systemctl", action, "kerio-kvc"], capture_output=True, text=True)
        if res.returncode == 0:
            print(f"Команда '{{action}}' успешно выполнена.")
        else:
            print(f"[ОШИБКА] systemctl {{action}} завершился с кодом {{res.returncode}}: {{res.stderr.strip()}}", file=sys.stderr)
            sys.exit(res.returncode)
    except Exception as e:
        print(f"[ОШИБКА] Ошибка выполнения systemctl: {{e}}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
"""

        # Создаем временный файл скрипта
        temp_script = tempfile.NamedTemporaryFile(delete=False, suffix=".py", mode="w", encoding="utf-8")
        temp_script.write(helper_code)
        temp_script.close()

        self.log_area.append(f"--- Запуск операции '{action}' ---")

        # Единственный вызов pkexec с передачей аргументов
        import subprocess
        try:
            res = subprocess.run([
                "pkexec", "python3", temp_script.name, action, server, login, password
            ], capture_output=True, text=True)
            
            # Удаляем временный файл скрипта сразу же
            if os.path.exists(temp_script.name):
                os.unlink(temp_script.name)
                
            # Отображаем вывод скрипта в текстовой области
            if res.stdout:
                self.log_area.append(res.stdout.strip())
            if res.stderr:
                self.log_area.append(res.stderr.strip())

            # Обновляем статус службы и логи в GUI
            self.check_service_status()
            self.fetch_logs()

            return res.returncode == 0
        except Exception as e:
            self.log_area.append(f"[ОШИБКА] Исключение при выполнении pkexec: {str(e)}")
            if os.path.exists(temp_script.name):
                os.unlink(temp_script.name)
            return False

    # --- Работа с профилями ---

    def load_profiles_to_combo(self):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        profiles = self.settings.childGroups()
        if not profiles:
            self.profile_combo.addItem("— Выберите профиль —")
        else:
            self.profile_combo.addItems(profiles)
        self.profile_combo.blockSignals(False)

    def auto_load_first_profile(self):
        """Автоматическая загрузка первого профиля при запуске."""
        profiles = self.settings.childGroups()
        if profiles:
            first_profile = profiles[0]
            self.profile_combo.setCurrentText(first_profile)
            self.apply_profile_data(first_profile)

    def on_profile_selected(self):
        profile_name = self.profile_combo.currentText()
        if not profile_name or profile_name == "— Выберите профиль —":
            return
        self.apply_profile_data(profile_name)

    def apply_profile_data(self, profile_name):
        """Загружает данные профиля в поля."""
        self.settings.beginGroup(profile_name)
        server = self.settings.value("server", "")
        login = self.settings.value("login", "")
        password = self.settings.value("password", "")
        self.settings.endGroup()

        self.server_input.setText(server)
        self.login_input.setText(login)
        self.pass_input.setText(password)

        self.status_bar.showMessage(f"Загружен профиль: {profile_name}")
        self.log_area.append(f"--- Загружен профиль '{profile_name}' ---")

    def save_profile(self):
        current_name = self.profile_combo.currentText()
        if current_name == "— Выберите профиль —":
            current_name = ""

        profile_name, ok = QInputDialog.getText(
            self, "Сохранение профиля", "Введите название профиля:",
            QLineEdit.EchoMode.Normal, current_name
        )

        if ok and profile_name.strip():
            profile_name = profile_name.strip()
            self.settings.beginGroup(profile_name)
            self.settings.setValue("server", self.server_input.text().strip())
            self.settings.setValue("login", self.login_input.text().strip())
            self.settings.setValue("password", self.pass_input.text().strip())
            self.settings.endGroup()

            self.load_profiles_to_combo()
            self.profile_combo.setCurrentText(profile_name)

            # Применение сразу при сохранении
            self.apply_profile_data(profile_name)

    def delete_profile(self):
        profile_name = self.profile_combo.currentText()
        if not profile_name or profile_name == "— Выберите профиль —":
            return

        if QMessageBox.question(self, "Удаление",
                                f"Удалить профиль '{profile_name}'?") == QMessageBox.StandardButton.Yes:
            self.settings.remove(profile_name)
            self.load_profiles_to_combo()

    # --- Управление systemctl через pkexec ---

    def run_pkexec_systemctl(self, action):
        cmd = f"pkexec systemctl {action} kerio-kvc"
        self.log_area.append(f"--- Выполнение: {cmd} ---")

        exit_code = os.system(cmd)
        if exit_code == 0:
            self.log_area.append(f"Команда '{action}' успешно выполнена.")
        else:
            self.log_area.append(f"[ОШИБКА] Не удалось выполнить '{action}' (код: {exit_code}).")

        self.check_service_status()
        self.fetch_logs()

    def start_service(self):
        server = self.server_input.text().strip()
        login = self.login_input.text().strip()
        password = self.pass_input.text().strip()
        self.backup_update_and_run_systemctl("start", server, login, password)

    def stop_service(self):
        self.run_pkexec_systemctl("stop")

    def restart_service(self):
        server = self.server_input.text().strip()
        login = self.login_input.text().strip()
        password = self.pass_input.text().strip()
        self.backup_update_and_run_systemctl("restart", server, login, password)

    def check_service_status(self):
        process = QProcess()
        process.start("systemctl", ["is-active", "kerio-kvc"])
        process.waitForFinished(1000)

        status = process.readAllStandardOutput().data().decode("utf-8").strip()
        if status == "active":
            self.status_label.setText("Статус: ПОДКЛЮЧЕНО (active)")
            self.status_label.setStyleSheet("color: green; font-weight: bold;")
            self.btn_start.setEnabled(False)
            self.btn_stop.setEnabled(True)
        else:
            self.status_label.setText(f"Статус: ОТКЛЮЧЕНО ({status})")
            self.status_label.setStyleSheet("color: red; font-weight: bold;")
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)

    def fetch_logs(self):
        self.log_process.start("journalctl", ["-u", "kerio-kvc", "-n", "50", "--no-pager"])

    def handle_log_output(self):
        data = self.log_process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        self.log_area.setText(data.strip())

    # --- Системный трей ---

    def init_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QIcon.fromTheme("network-vpn", self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon)))
        self.tray_icon.activated.connect(self.on_tray_icon_activated)

        self.tray_menu = QMenu()
        self.tray_menu.aboutToShow.connect(self.update_tray_menu)
        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.show()

    def on_tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible():
                self.hide()
            else:
                self.show_normal()

    def show_normal(self):
        self.showNormal()
        self.activateWindow()

    def update_tray_menu(self):
        self.tray_menu.clear()

        # Список профилей по правому клику
        profiles = self.settings.childGroups()
        if profiles:
            self.tray_menu.addSection("Профили")
            for profile in profiles:
                action = self.tray_menu.addAction(profile)
                if profile == self.profile_combo.currentText():
                    action.setCheckable(True)
                    action.setChecked(True)
                action.triggered.connect(lambda checked, p=profile: self.select_profile_from_tray(p))
            self.tray_menu.addSeparator()

        # Основные действия
        show_action = self.tray_menu.addAction("Открыть")
        show_action.triggered.connect(self.show_normal)
        self.tray_menu.addSeparator()

        start_action = self.tray_menu.addAction("Запустить службу")
        start_action.triggered.connect(self.start_service)

        stop_action = self.tray_menu.addAction("Остановить службу")
        stop_action.triggered.connect(self.stop_service)
        self.tray_menu.addSeparator()

        quit_action = self.tray_menu.addAction("Выход")
        quit_action.triggered.connect(QApplication.instance().quit)

    def select_profile_from_tray(self, profile_name):
        index = self.profile_combo.findText(profile_name)
        if index >= 0:
            self.profile_combo.setCurrentIndex(index)
        else:
            self.apply_profile_data(profile_name)
        self.show_normal()

    def closeEvent(self, event):
        if self.tray_icon.isVisible():
            self.hide()
            event.ignore()


def main():
    QLoggingCategory.setFilterRules("qt.svg.draw=false")
    
    # Задаем имя процесса для корректного WM_CLASS в Linux (X11/Wayland)
    sys.argv[0] = "kerio-kvc-gui"
    
    app = QApplication(sys.argv)
    app.setApplicationName("kerio-kvc-gui")
    app.setApplicationDisplayName("KVC GUI")
    app.setDesktopFileName("kerio-kvc-gui")

    # Инициализация файла блокировки для предотвращения повторного запуска
    lock_file_path = os.path.join(tempfile.gettempdir(), "kerio_kvc_gui.lock")
    lock_file = QLockFile(lock_file_path)
    if not lock_file.tryLock(100):
        QMessageBox.warning(
            None,
            "Ошибка запуска",
            "Приложение Kerio Control VPN Client GUI уже запущено."
        )
        sys.exit(1)

    app.setQuitOnLastWindowClosed(False)
    window = KerioKvcGUI()
    # Сохраняем ссылку на lock_file в объекте окна, чтобы предотвратить сборку мусора (GC)
    window.lock_file = lock_file
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()