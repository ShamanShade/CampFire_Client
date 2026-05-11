from PyQt6.QtWidgets import *
from PyQt6.QtGui import QPixmap, QIcon
from PyQt6.QtCore import QSize, Qt
import socket
import threading
import base64

HOST = 'localhost'
PORT = 55555

client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)


class SettingsWindow(QWidget):
    def __init__(self, parent):
        super().__init__()

        self.setWindowTitle("Settings")
        self.setFixedSize(300, 150)

        self.parent = parent

        layout = QVBoxLayout()

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Username")

        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self.save)

        layout.addWidget(QLabel("Settings"))
        layout.addWidget(self.name_input)
        layout.addWidget(self.save_btn)

        self.setLayout(layout)

    def save(self):
        name = self.name_input.text().strip()

        if name:
            self.parent.username = name

            try:
                client.send(f"/username {name}".encode())
            except:
                pass

        self.close()


class ChatApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Campfire")
        self.resize(1000, 600)
        self.setWindowIcon(QIcon("icon.png"))

        self.username = "You"
        self.current_room = "#general"

        # ---------------- MAIN LAYOUT
        layout = QHBoxLayout()
        self.setLayout(layout)

        # ---------------- LEFT PANEL (Spotify style)
        left_panel_layout = QVBoxLayout()

        self.app_icon = QLabel()
        pixmap = QPixmap("icon.png")
        self.app_icon.setPixmap(
            pixmap.scaled(
                80, 80,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
        )
        self.app_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.sidebar = QListWidget()
        self.sidebar.setMaximumWidth(220)

        self.sidebar.addItem("#general")
        self.sidebar.addItem("#gaming")
        self.sidebar.addItem("DM: Friend1")
        self.sidebar.addItem("DM: Friend2")

        self.sidebar.itemClicked.connect(self.change_room)

        left_panel_layout.addWidget(self.app_icon)
        left_panel_layout.addWidget(self.sidebar)

        left_widget = QWidget()
        left_widget.setLayout(left_panel_layout)

        # ---------------- CHAT AREA
        center = QVBoxLayout()

        self.chat_box = QTextEdit()
        self.chat_box.setReadOnly(True)

        self.input_box = QLineEdit()
        self.input_box.setPlaceholderText("Message...")
        self.input_box.returnPressed.connect(self.send_message)

        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send_message)

        self.settings_btn = QPushButton("Settings")
        self.settings_btn.clicked.connect(self.open_settings)

        center.addWidget(self.chat_box)
        center.addWidget(self.input_box)
        center.addWidget(self.send_btn)
        center.addWidget(self.settings_btn)

        center_widget = QWidget()
        center_widget.setLayout(center)

        # ---------------- USERS PANEL
        self.users_list = QListWidget()
        self.users_list.setMaximumWidth(220)
        self.users_list.setIconSize(QSize(32, 32))
        self.users_list.addItem("Online Users")

        # ---------------- ADD TO MAIN LAYOUT
        layout.addWidget(left_widget, 0)
        layout.addWidget(center_widget, 1)
        layout.addWidget(self.users_list, 0)

        # ---------------- CONNECT (threaded so UI doesn't freeze)
        threading.Thread(target=self.connect_to_server, daemon=True).start()

    # ---------------- CONNECT
    def connect_to_server(self):
        try:
            client.connect((HOST, PORT))

            client.recv(1024)
            client.send(self.username.encode())

            # avatar
            avatar_path = "avatar.png"
            try:
                with open(avatar_path, "rb") as f:
                    avatar_b64 = base64.b64encode(f.read()).decode()
            except:
                avatar_b64 = ""

            client.recv(1024)
            client.send(avatar_b64.encode())

            client.recv(1024)
            client.send(self.current_room.encode())

            threading.Thread(target=self.receive_messages, daemon=True).start()

        except Exception as e:
            print("Connection failed:", e)

    # ---------------- RECEIVE
    def receive_messages(self):
        while True:
            try:
                data = client.recv(4096).decode()
                if not data:
                    break

                if data.startswith("MSG|"):
                    _, user, msg = data.split("|", 2)
                    self.chat_box.append(f"{user}: {msg}")

                elif data.startswith("USERLIST|"):
                    _, users = data.split("|", 1)
                    self.update_users(users.split(",") if users else [])

            except:
                break

    # ---------------- SEND
    def send_message(self):
        msg = self.input_box.text().strip()
        if not msg:
            return

        try:
            client.send(msg.encode())
        except:
            self.chat_box.append("Failed to send message")

        self.input_box.clear()

    # ---------------- USERS (FIXED)
    def update_users(self, users):
        self.users_list.clear()
        self.users_list.addItem("Online Users")
        self.users_list.setIconSize(QSize(32, 32))

        for u in users:
            if not u:
                continue

            try:
                name, avatar_b64 = u.split("::", 1)
            except:
                continue

            item = QListWidgetItem(name)

            pixmap = QPixmap()

            try:
                if avatar_b64:
                    img = base64.b64decode(avatar_b64)
                    ok = pixmap.loadFromData(img)

                    if not ok:
                        pixmap = QPixmap("avatar.png")
                else:
                    pixmap = QPixmap("avatar.png")
            except:
                pixmap = QPixmap("avatar.png")

            item.setIcon(QIcon(pixmap))
            self.users_list.addItem(item)

    # ---------------- ROOM
    def change_room(self, item):
        self.current_room = item.text()

        try:
            client.send(f"/room {self.current_room}".encode())
        except:
            self.chat_box.append("Room switch failed")

        self.chat_box.clear()
        self.chat_box.append(f"Switched to {self.current_room}")

    # ---------------- SETTINGS
    def open_settings(self):
        self.settings = SettingsWindow(self)
        self.settings.show()


app = QApplication([])
window = ChatApp()
window.show()
app.exec()