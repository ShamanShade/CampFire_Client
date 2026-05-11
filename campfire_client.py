from PyQt6.QtWidgets import *
from PyQt6.QtGui import QPixmap, QIcon
from PyQt6.QtCore import pyqtSignal, QObject, Qt
import socket
import threading
import base64

# ================= SERVER CONFIG
HOST = "74.132.179.199"   # change if needed
PORT = 55555

client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)


# ================= SIGNALS (THREAD SAFE)
class Signals(QObject):
    message = pyqtSignal(str)
    users = pyqtSignal(list)
    friend_request = pyqtSignal(str)
    open_chat = pyqtSignal(str)


signals = Signals()


# ================= SAFE RECEIVE (LINE BASED PROTOCOL)
def recv_line(sock):
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(1)
        if not chunk:
            return ""
        data += chunk
    return data.decode().strip()


# ================= LOGIN WINDOW
class LoginWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Campfire Login")
        self.setFixedSize(300, 200)

        layout = QVBoxLayout()

        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("Username")

        self.pass_input = QLineEdit()
        self.pass_input.setPlaceholderText("Password")
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)

        self.btn = QPushButton("Login / Register")
        self.btn.clicked.connect(self.login)

        self.status = QLabel("")

        layout.addWidget(self.user_input)
        layout.addWidget(self.pass_input)
        layout.addWidget(self.btn)
        layout.addWidget(self.status)

        self.setLayout(layout)

        signals.open_chat.connect(self.open_chat)

    def login(self):
        username = self.user_input.text().strip()
        password = self.pass_input.text().strip()

        if not username or not password:
            self.status.setText("Fill both fields")
            return

        try:
            client.connect((HOST, PORT))
        except Exception as e:
            self.status.setText(f"Connect failed: {e}")
            return

        def login_flow():
            try:
                msg = recv_line(client)
                if msg != "LOGIN":
                    self.status.setText("Protocol error")
                    return

                client.send(f"{username}:{password}\n".encode())

                resp = recv_line(client)
                if resp != "LOGIN_OK":
                    self.status.setText("Login failed")
                    return

                recv_line(client)  # AVATAR prompt
                client.send("\n".encode())

                recv_line(client)  # ROOM prompt
                client.send("#general\n".encode())

                signals.open_chat.emit(username)

            except Exception as e:
                self.status.setText(str(e))

        threading.Thread(target=login_flow, daemon=True).start()

    def open_chat(self, username):
        self.chat = ChatApp(username)
        self.chat.show()
        self.close()


# ================= SETTINGS WINDOW
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
                client.send(f"/username {name}\n".encode())
            except:
                pass

        self.close()


# ================= MAIN CHAT APP (MERGED VERSION)
class ChatApp(QWidget):
    def __init__(self, username):
        super().__init__()

        self.username = username
        self.current_room = "#general"

        self.setWindowTitle(f"Campfire - {username}")
        self.resize(1100, 650)
        self.setWindowIcon(QIcon("icon.png"))

        layout = QHBoxLayout()
        self.setLayout(layout)

        # ================= LEFT PANEL
        left = QVBoxLayout()

        self.app_icon = QLabel()
        pixmap = QPixmap("icon.png")
        self.app_icon.setPixmap(
            pixmap.scaled(80, 80, Qt.AspectRatioMode.KeepAspectRatio)
        )
        self.app_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.sidebar = QListWidget()
        self.sidebar.setMaximumWidth(220)
        self.sidebar.addItems(["#general", "#gaming"])
        self.sidebar.itemClicked.connect(self.change_room)

        left.addWidget(self.app_icon)
        left.addWidget(QLabel("Channels"))
        left.addWidget(self.sidebar)

        left_widget = QWidget()
        left_widget.setLayout(left)

        # ================= CENTER CHAT
        center = QVBoxLayout()

        self.chat = QTextEdit()
        self.chat.setReadOnly(True)

        self.input = QLineEdit()
        self.input.setPlaceholderText("Message...")
        self.input.returnPressed.connect(self.send)

        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send)

        self.settings_btn = QPushButton("Settings")
        self.settings_btn.clicked.connect(self.open_settings)

        center.addWidget(self.chat)
        center.addWidget(self.input)
        center.addWidget(self.send_btn)
        center.addWidget(self.settings_btn)

        center_widget = QWidget()
        center_widget.setLayout(center)

        # ================= RIGHT PANEL
        right = QVBoxLayout()

        self.users_list = QListWidget()
        self.requests_list = QListWidget()

        right.addWidget(QLabel("Online Users"))
        right.addWidget(self.users_list)

        right.addWidget(QLabel("Friend Requests"))
        right.addWidget(self.requests_list)

        right_widget = QWidget()
        right_widget.setLayout(right)

        # ================= MAIN LAYOUT
        layout.addWidget(left_widget)
        layout.addWidget(center_widget, 1)
        layout.addWidget(right_widget)

        # ================= SIGNALS
        signals.message.connect(self.chat.append)
        signals.users.connect(self.update_users)
        signals.friend_request.connect(self.add_friend_request)

        # ================= THREAD
        threading.Thread(target=self.receive, daemon=True).start()

    # ================= RECEIVE LOOP
    def receive(self):
        while True:
            try:
                data = recv_line(client)
                if not data:
                    break

                if data.startswith("MSG|"):
                    _, user, msg = data.split("|", 2)
                    signals.message.emit(f"{user}: {msg}")

                elif data.startswith("USERLIST|"):
                    _, users = data.split("|", 1)
                    signals.users.emit(users.split(",") if users else [])

                elif data.startswith("FRIEND_REQUEST|"):
                    _, user = data.split("|", 1)
                    signals.friend_request.emit(user)

                elif data.startswith("DM|"):
                    _, user, msg = data.split("|", 2)
                    signals.message.emit(f"[DM] {user}: {msg}")

            except:
                break

    # ================= SEND MESSAGE
    def send(self):
        msg = self.input.text().strip()
        if not msg:
            return

        try:
            client.send((msg + "\n").encode())
        except:
            signals.message.emit("Send failed")

        self.input.clear()

    # ================= USERS
    def update_users(self, users):
        self.users_list.clear()

        for u in users:
            if not u:
                continue

            try:
                name, avatar = u.split("::", 1)
            except:
                continue

            item = QListWidgetItem(name)

            pixmap = QPixmap()
            try:
                if avatar:
                    pixmap.loadFromData(base64.b64decode(avatar))
                else:
                    pixmap = QPixmap("avatar.png")
            except:
                pixmap = QPixmap("avatar.png")

            item.setIcon(QIcon(pixmap))
            self.users_list.addItem(item)

    # ================= FRIEND REQUEST
    def add_friend_request(self, user):
        self.requests_list.addItem(user)

    # ================= ROOM SWITCH
    def change_room(self, item):
        room = item.text()
        self.current_room = room

        try:
            client.send(f"/room {room}\n".encode())
        except:
            pass

        self.chat.clear()
        signals.message.emit(f"Switched to {room}")

    # ================= SETTINGS
    def open_settings(self):
        self.s = SettingsWindow(self)
        self.s.show()


# ================= START APP
app = QApplication([])

login = LoginWindow()
login.show()

app.exec()