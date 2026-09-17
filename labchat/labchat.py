#!/usr/bin/env python3
"""
LabChat
A zero-dependency Python terminal chat application for local networks.
"""

import os
import sys
import time
import json
import uuid
import socket
import select
import argparse
import platform
import threading
import ipaddress
import concurrent.futures
from datetime import datetime

DEFAULT_PORT = 50555
CONFIG_DIR = os.path.expanduser("~/.labchat")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


def get_local_ip():
    """Determine the local IPv4 address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_subnet(ip):
    """Get the /24 subnet for the given IP address."""
    try:
        if ip == "127.0.0.1":
            return None
        return ipaddress.IPv4Network(f"{ip}/24", strict=False)
    except Exception:
        return None


class Config:
    @staticmethod
    def load():
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    @staticmethod
    def save(config_dict):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config_dict, f, indent=4)
        except Exception as e:
            print(f"Error saving config: {e}")


class Connection:
    def __init__(self, sock, addr, app, is_server=False):
        self.sock = sock
        self.addr = addr
        self.app = app
        self.is_server = is_server
        self.peer_name = "Unknown"
        self.peer_id = None
        self.running = True
        
        # Start a background thread to read from this connection
        threading.Thread(target=self.read_loop, daemon=True).start()

    def read_loop(self):
        try:
            f = self.sock.makefile('r', encoding='utf-8')
            while self.running:
                line = f.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                    self.app.handle_message(self, msg)
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass
        finally:
            self.close()
            self.app.handle_disconnect(self)

    def send(self, msg_dict):
        if not self.running:
            return False
        try:
            data = json.dumps(msg_dict) + "\n"
            self.sock.sendall(data.encode('utf-8'))
            return True
        except Exception:
            self.close()
            return False

    def close(self):
        if self.running:
            self.running = False
            try:
                self.sock.close()
            except Exception:
                pass


class LabChat:
    def __init__(self, name=None, port=None, initial_scan=False):
        self.config = Config.load()
        
        self.name = name or self.config.get("name")
        self.port = port or self.config.get("port", DEFAULT_PORT)
        
        self.instance_id = str(uuid.uuid4())
        self.local_ip = get_local_ip()
        
        self.peers = {}  # id -> {name, ip, port, last_seen}
        self.connections = []  # list of Connection objects
        
        self.running = False
        self.server_sock = None
        self.udp_sock = None
        
        self.initial_scan = initial_scan
        
        if os.name == 'nt':
            os.system('color')  # Enable ANSI in windows cmd

    def prompt_name(self):
        print("╔══════════════════════════════╗")
        print("║          LABCHAT             ║")
        print("╚══════════════════════════════╝")
        while not self.name:
            try:
                name_input = input("Enter your name: ").strip()
                if name_input:
                    self.name = name_input
            except (KeyboardInterrupt, EOFError):
                sys.exit(0)
        
        self.config["name"] = self.name
        self.config["port"] = self.port
        Config.save(self.config)
        print(f"\nWelcome, {self.name}!\n")

    def print_msg(self, text):
        # Clear the current line and rewrite prompt to avoid mangled inputs
        sys.stdout.write("\r\033[2K" + text + "\n> ")
        sys.stdout.flush()

    def handle_message(self, conn, msg):
        msg_type = msg.get("type")
        
        if msg_type == "hello":
            conn.peer_name = msg.get("name", "Unknown")
            conn.peer_id = msg.get("instance_id")
            
            # Record peer
            if conn.peer_id:
                self.add_peer({
                    "name": conn.peer_name,
                    "ip": conn.addr[0],
                    "port": msg.get("port", self.port),
                    "instance_id": conn.peer_id
                })
            
            if conn.is_server:
                # If we received a connection, send our hello back
                conn.send({
                    "type": "hello",
                    "name": self.name,
                    "instance_id": self.instance_id,
                    "port": self.port
                })
                
        elif msg_type == "message":
            sender = msg.get("sender", "Unknown")
            text = msg.get("message", "")
            timestamp = msg.get("timestamp", int(time.time()))
            dt = datetime.fromtimestamp(timestamp)
            time_str = dt.strftime("%H:%M")
            self.print_msg(f"[{time_str}] {sender}: {text}")

    def handle_disconnect(self, conn):
        if conn in self.connections:
            self.connections.remove(conn)
            time_str = datetime.now().strftime("%H:%M")
            if conn.peer_name:
                self.print_msg(f"[{time_str}] {conn.peer_name} disconnected.")

    def add_peer(self, peer_data):
        peer_id = peer_data.get("instance_id")
        if peer_id and peer_id != self.instance_id:
            peer_data["last_seen"] = time.time()
            self.peers[peer_id] = peer_data

    def start_tcp_server(self):
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.server_sock.bind(("", self.port))
            self.server_sock.listen(5)
        except Exception as e:
            print(f"Failed to start TCP server on port {self.port}: {e}")
            sys.exit(1)
            
        def accept_loop():
            while self.running:
                try:
                    sock, addr = self.server_sock.accept()
                    conn = Connection(sock, addr, self, is_server=True)
                    self.connections.append(conn)
                except Exception:
                    pass
                    
        threading.Thread(target=accept_loop, daemon=True).start()

    def start_udp_discovery(self):
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, 'SO_REUSEPORT'):
            try:
                self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except AttributeError:
                pass
                
        try:
            self.udp_sock.bind(("", self.port))
        except Exception:
            pass # Ignore UDP bind failures if port is already in use by another local instance
            
        def announce_loop():
            while self.running:
                msg = {
                    "type": "announce",
                    "name": self.name,
                    "ip": self.local_ip,
                    "port": self.port,
                    "instance_id": self.instance_id
                }
                data = json.dumps(msg).encode('utf-8')
                
                try:
                    self.udp_sock.sendto(data, ('<broadcast>', self.port))
                except Exception:
                    pass
                    
                try:
                    subnet = get_subnet(self.local_ip)
                    if subnet:
                        bcast = str(subnet.broadcast_address)
                        self.udp_sock.sendto(data, (bcast, self.port))
                except Exception:
                    pass
                    
                time.sleep(10)
                
        def listen_loop():
            while self.running:
                try:
                    data, addr = self.udp_sock.recvfrom(4096)
                    msg = json.loads(data.decode('utf-8'))
                    if msg.get("type") == "announce":
                        self.add_peer(msg)
                except Exception:
                    time.sleep(1)
                    
        threading.Thread(target=announce_loop, daemon=True).start()
        threading.Thread(target=listen_loop, daemon=True).start()

    def scan_subnet(self):
        subnet = get_subnet(self.local_ip)
        if not subnet:
            self.print_msg("Could not determine local subnet.")
            return
            
        self.print_msg(f"Scanning {subnet}...")
        
        def check_ip(ip_obj):
            ip = str(ip_obj)
            if ip == self.local_ip:
                return
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.5)
                res = s.connect_ex((ip, self.port))
                if res == 0:
                    # Send hello
                    msg = {
                        "type": "hello",
                        "name": self.name,
                        "instance_id": self.instance_id,
                        "port": self.port
                    }
                    s.sendall((json.dumps(msg) + "\n").encode('utf-8'))
                    
                    # Wait for response
                    f = s.makefile('r', encoding='utf-8')
                    line = f.readline()
                    if line:
                        resp = json.loads(line)
                        if resp.get("type") == "hello":
                            self.add_peer({
                                "name": resp.get("name", "Unknown"),
                                "ip": ip,
                                "port": self.port,
                                "instance_id": resp.get("instance_id")
                            })
                s.close()
            except Exception:
                pass
                
        ips = list(subnet.hosts())
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            executor.map(check_ip, ips)
            
        self.print_msg("Scan complete.")

    def connect_to(self, ip, port):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect((ip, port))
            s.settimeout(None)
            
            conn = Connection(s, (ip, port), self, is_server=False)
            self.connections.append(conn)
            
            conn.send({
                "type": "hello",
                "name": self.name,
                "instance_id": self.instance_id,
                "port": self.port
            })
            
            self.print_msg(f"Connected to {ip}:{port}.")
        except Exception as e:
            self.print_msg(f"Could not connect to {ip}:{port}\nPossible causes:\n- LabChat is not running\n- Firewall is blocking the port\n- Devices are isolated from each other\n- Incorrect IP or port\nError: {e}")

    def show_users(self):
        self.print_msg("\nONLINE USERS\n" + "─"*47)
        users = list(self.peers.values())
        # Filter out old users (not seen in 30 seconds)
        current_time = time.time()
        active_users = [u for u in users if (current_time - u.get("last_seen", 0)) < 30]
        
        if not active_users:
            self.print_msg("No users found. Try /scan to search the local network.\n")
            return
            
        for i, u in enumerate(active_users):
            self.print_msg(f"[{i+1}] {u['name']:<10} {u['ip']}:{u['port']}")
        self.print_msg("")

    def show_help(self):
        help_text = """
Commands:
/users                 Show discovered users
/discover, /scan       Scan local subnet
/connect <ip[:port]>   Connect to an IP address (or /connect <id> from /users list)
/disconnect            Disconnect from all active chats
/name <new_name>       Change your name
/status                Show network status
/server                Show configured server/active connections
/port                  Show current port
/clear                 Clear terminal
/help                  Show help
/quit, /exit           Exit LabChat
"""
        self.print_msg(help_text)

    def print_ui_header(self):
        subnet = get_subnet(self.local_ip)
        print("╔══════════════════════════════════════════════╗")
        print("║                  LABCHAT                     ║")
        print("╚══════════════════════════════════════════════╝")
        print(f"\nYou are: {self.name}")
        print(f"Address: {self.local_ip}:{self.port}")
        if subnet:
            print(f"Subnet:  {subnet}")
        print("\nType /help for a list of commands.")
        print("───────────────────────────────────────────────")

    def run(self):
        if not self.name:
            self.prompt_name()
            
        self.running = True
        self.start_tcp_server()
        self.start_udp_discovery()
        
        self.print_ui_header()
        
        if self.initial_scan:
            print("Scanning local network...")
            self.scan_subnet()
            
        try:
            while self.running:
                try:
                    text = input("> ").strip()
                    self.handle_input(text)
                except EOFError:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            for conn in self.connections:
                conn.close()
            if self.server_sock:
                self.server_sock.close()
            if self.udp_sock:
                self.udp_sock.close()

    def handle_input(self, text):
        if not text:
            return
            
        if text.startswith("/"):
            parts = text.split(" ")
            cmd = parts[0].lower()
            
            if cmd == "/help":
                self.show_help()
            elif cmd == "/users":
                self.show_users()
            elif cmd in ("/discover", "/scan"):
                threading.Thread(target=self.scan_subnet, daemon=True).start()
            elif cmd == "/connect":
                if len(parts) > 1:
                    addr = parts[1]
                    if addr.isdigit():
                        idx = int(addr)
                        # Filter to active only for indices
                        current_time = time.time()
                        active_users = [u for u in self.peers.values() if (current_time - u.get("last_seen", 0)) < 30]
                        if 1 <= idx <= len(active_users):
                            u = active_users[idx-1]
                            self.connect_to(u['ip'], u['port'])
                        else:
                            self.print_msg("Invalid user index. Check /users.")
                    else:
                        ip = addr
                        port = self.port
                        if ":" in addr:
                            try:
                                ip, p = addr.split(":", 1)
                                port = int(p)
                            except ValueError:
                                pass
                        self.connect_to(ip, port)
                else:
                    self.print_msg("Usage: /connect <ip[:port]> or /connect <id>")
            elif cmd == "/disconnect":
                for c in self.connections:
                    c.close()
                self.connections.clear()
                self.print_msg("Disconnected from all peers.")
            elif cmd == "/name":
                if len(parts) > 1:
                    self.name = " ".join(parts[1:])
                    self.config["name"] = self.name
                    Config.save(self.config)
                    self.print_msg(f"Name changed to {self.name}")
                else:
                    self.print_msg("Usage: /name <new_name>")
            elif cmd == "/status":
                self.print_msg(f"Name: {self.name}\nIP: {self.local_ip}\nPort: {self.port}\nConnections: {len(self.connections)}")
            elif cmd == "/server":
                if self.connections:
                    self.print_msg("Active connections:")
                    for c in self.connections:
                        name = c.peer_name if c.peer_name else "Unknown"
                        self.print_msg(f"- {name} ({c.addr[0]}:{c.addr[1]})")
                else:
                    self.print_msg("No active connections.")
            elif cmd == "/port":
                self.print_msg(f"Current port: {self.port}")
            elif cmd == "/clear":
                os.system('cls' if os.name == 'nt' else 'clear')
                self.print_ui_header()
            elif cmd in ("/quit", "/exit"):
                self.running = False
            else:
                self.print_msg(f"Unknown command: {cmd}")
        else:
            if not self.connections:
                self.print_msg("You are not connected to anyone. Use /connect <ip> or type /users to see discovered users.")
            else:
                self.send_chat_message(text)

    def send_chat_message(self, text):
        msg = {
            "type": "message",
            "id": str(uuid.uuid4()),
            "sender": self.name,
            "timestamp": int(time.time()),
            "message": text
        }
        
        time_str = datetime.now().strftime("%H:%M")
        
        # Clear line to print "You: " nicely above the prompt
        sys.stdout.write("\r\033[2K" + f"[{time_str}] You: {text}\n> ")
        sys.stdout.flush()
        
        disconnected = []
        for conn in self.connections:
            if not conn.send(msg):
                disconnected.append(conn)
                
        for conn in disconnected:
            self.handle_disconnect(conn)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LabChat - Zero-dependency LAN chat")
    parser.add_argument("--name", type=str, help="Your display name")
    parser.add_argument("--port", type=int, help="Port to use (default: 50555)")
    parser.add_argument("--scan", action="store_true", help="Perform a subnet scan on startup")
    
    args = parser.parse_args()
    
    app = LabChat(name=args.name, port=args.port, initial_scan=args.scan)
    app.run()
