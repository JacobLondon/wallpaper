from collections import defaultdict
import argparse
import ctypes, win32con
import json
import os
import random
import threading
import time
import socket
import socketserver

def get_wallpaper():
    ubuf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.SystemParametersInfoW(win32con.SPI_GETDESKWALLPAPER,len(ubuf),ubuf,0)
    return ubuf.value

def set_wallpaper(path):
    changed = win32con.SPIF_UPDATEINIFILE | win32con.SPIF_SENDCHANGE
    ctypes.windll.user32.SystemParametersInfoW(win32con.SPI_SETDESKWALLPAPER,0,path,changed)

def index(root_directory: str) -> list[str]:
    builder = []
    for root, dirs, filenames in os.walk(root_directory, followlinks=True):
        for filename in filenames:
            builder.append(os.path.join(root, filename))
    return builder

def histogram_extensions(filepaths: list[str]) -> dict[str, int]:
    histogram = defaultdict(int)
    for filepath in filepaths:
        _, extension = os.path.splitext(filepath)
        histogram[extension] += 1
    return histogram

class InputHost:
    def __init__(self, manager):
        self.manager: WallpaperManager = manager
        self.mode_favorites = False
    def read(self, command):
        command = command.lower()
        if command in ('s', 'sk', 'ski', 'skip'):
            self.manager.dislike()
        elif command in ('u', 'un', 'und', 'undo'):
            self.manager.undo()
        elif command in ('n', 'ne', 'nex', 'next'):
            self.manager.pick(favorites=self.mode_favorites)
        elif command in ('l', 'li', 'lis', 'list'):
            print("Mode: %s" % ("Fav" if self.mode_favorites else "Normal"))
            print("Wall:", self.manager.get_chosen())
        elif command in ('f', 'fa', 'fav'):
            self.manager.favorite()
        elif command.startswith("mode"):
            lineargs = command.split()
            if len(lineargs) == 1:
                return True

            arg2 = lineargs[1].lower()
            if arg2 == "normal":
                self.mode_favorites = False
            elif arg2 == "fav":
                self.mode_favorites = True

        elif command in ('exit', 'quit', 'done'):
            self.manager.set_done()
            return False

        elif command == 'noop':
            return True

        # keep going otherwise
        return True

class InputStdin(InputHost):
    def read(self, *args, **kwargs):
        try:
            command = input("{Skip|Undo|Next|List|Fav|Mode-[Normal|Fav]}>> ")
        except KeyboardInterrupt:
            self.manager.set_done()
            return False
        return super().read(command)

class InputServer(InputHost):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.server_socket = None
        self.client_socket = None

    def __del__(self):
        server_socket = getattr(self, "server_socket", None)
        if server_socket:
            server_socket.close()

        client_socket = getattr(self, "client_socket", None)
        if client_socket:
            client_socket.close()

    def _open(self):
        if self.server_socket is not None:
            return

        #print("Opening...")
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.settimeout(5)
        host = '127.0.0.1'
        port = 30301
        self.server_socket.bind((host, port))
        self.server_socket.listen(1)

    def _accept(self):
        if self.server_socket is None:
            return
        if self.client_socket is not None:
            return

        try:
            #print("Accepting...", end='')
            self.client_socket, addr = self.server_socket.accept()
        except:
            #print("Nope!")
            self.client_socket = None
            return
        #print("OK")

    def _read(self):
        if self.client_socket is None:
            return 'noop'

        try:
            #print("Read...", end='')
            self.client_socket.settimeout(5)
            buffer = self.client_socket.recv(1024)
        except:
            self.client_socket.close()
            self.client_socket = None
            #print("NOPE!")
            return 'noop'

        command = str(buffer, encoding="utf8")
        if len(command) == 0:
            self.client_socket.close()
            self.client_socket = None
            return 'noop'

        return command

    def read(self, *args, **kwargs):
        try:
            self._open()
            self._accept()
            command = self._read()
            return super().read(command)
        except KeyboardInterrupt:
            self.manager.set_done()
            return 'noop'

class Client:
    def __init__(self):
        self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        host = '127.0.0.1'
        port = 30301
        self.client_socket.connect((host, port))

    def __del__(self):
        client_socket = getattr(self, "client_socket", None)
        if client_socket:
            client_socket.close()

    def send(self, command):
        buf = bytes(command, encoding='utf8')
        self.client_socket.sendall(buf)

class WallpaperManager:
    CHECKPOINT_DISLIKED: str = "DislikedCheckpoint.json"
    CHECKPOINT_MANAGER: str = "ManagerCheckpoint.json"
    FAVORITES: str = "Favorites.json"

    @staticmethod
    def open(save_directory: str, file_out=None) -> 'WallpaperManager':
        manager_path = os.path.join(save_directory, WallpaperManager.CHECKPOINT_MANAGER)

        with open(manager_path, "r") as fp:
            return json.load(fp, object_hook=lambda d: WallpaperManager(
                root_directory=d["root_directory"],
                save_directory=d["save_directory"],
                legal_extensions=d["legal_extensions"],
                update_period_minutes=d["update_period_minutes"],
                file_out=file_out))

    def __init__(self, \
            root_directory: str, \
            save_directory: str, \
            legal_extensions: list[str], \
            update_period_minutes: int = 10,
            file_out=None):
        self.root_directory = root_directory
        self.save_directory = save_directory
        self.legal_extensions = legal_extensions
        self.update_period_minutes = update_period_minutes
        self.file_out = file_out

        self.lock = threading.Lock()

        indexed = index(self.root_directory)

        # only those with legal extensions
        filtered = []
        for filepath in indexed:
            for extension in self.legal_extensions:
                if filepath.endswith(extension):
                    filtered.append(filepath)
        indexed = filtered

        # and aren't disliked
        self.disliked_filepath = os.path.join(save_directory, WallpaperManager.CHECKPOINT_DISLIKED)
        with open(self.disliked_filepath, "r") as fp:
            self.disliked_filepaths = json.load(fp)

        indexed = list(filter(
            lambda filepath: filepath not in self.disliked_filepaths, indexed))

        # get favs
        self.favorites_filepath = os.path.join(save_directory, WallpaperManager.FAVORITES)
        with open(self.favorites_filepath, "r") as fp:
            self.favorites_filepaths = json.load(fp)

        # load 'em up
        self.indexed = indexed
        self.chosen = get_wallpaper()
        self.history = [self.chosen]
        self.done = False

    def undo(self):
        self.lock.acquire()

        if len(self.history) > 1:
            self.history.pop()
            top = self.history[-1]
            set_wallpaper(top)
            if self.file_out:
                print(top, file=self.file_out)

        self.lock.release()

    def favorite(self):
        self.lock.acquire()

        self.favorites_filepaths.append(self.chosen)

        try:
            if self.file_out:
                print(self.chosen, file=self.file_out)
            with open(self.favorites_filepath, "w") as fp:
                json.dump(self.favorites_filepaths, fp)
        finally:
            self.lock.release()

    def dislike(self):
        self.lock.acquire()

        if self.file_out:
            print(self.chosen, file=self.file_out)
        try:
            self.history.remove(self.chosen)
        except:
            pass
        self.indexed.remove(self.chosen)
        self.disliked_filepaths.append(self.chosen)
        try:
            with open(self.disliked_filepath, "w") as fp:
                json.dump([str(filepath) for filepath in self.disliked_filepaths], fp)
        finally:
            self.lock.release()

        self.pick()

    def pick(self, favorites=False):
        self.lock.acquire()

        if favorites and len(self.favorites_filepaths) > 0:
            choice = random.choice(self.favorites_filepaths)
        else:
            choice = random.choice(self.indexed)

        self.chosen = choice
        if self.file_out:
            print(self.chosen, file=self.file_out)
        set_wallpaper(self.chosen)
        self.history.append(self.chosen)
        self.lock.release()

    def get_chosen(self):
        return self.chosen

    def set_done(self):
        self.done = True

    def run(self):
        while not self.done:
            for _ in range(self.update_period_minutes):
                for _ in range(60//5):
                    if self.done: return
                    time.sleep(5)
            self.pick()

    def __str__(self):
        return "WallpaperManager%s" % str(self.__dict__)

    @staticmethod
    def ezrun(manager, input_cls, file_out=None):
        manager: WallpaperManager = manager
        input_method = input_cls(manager)

        thread = threading.Thread(target=manager.run)
        thread.start()

        keep_going = True
        while keep_going:
            keep_going = input_method.read()

        if file_out:
            print("Exiting. Please wait a moment...", file=file_out)
        thread.join()

def main():
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--index", action="store_true", help="Print out the file index")
    parser.add_argument("--histogram", action="store_true", help="Print the file extension histogram")
    parser.add_argument("--save", help="Specify a save directory containing %s" % WallpaperManager.CHECKPOINT_MANAGER)
    parser.add_argument("--client", action="store_true", help="Run as a client to a localhost server")
    parser.add_argument("--server", action="store_true", help="Run as a server as the input method")
    parser.add_argument("--csend", help="If a server is running, send the given command")

    args = parser.parse_args()

    if args.save:
        SAVE_DIRECTORY = args.save
    else:
        SAVE_DIRECTORY = r"F:\Workspaces\Python\wallpaper"

    manager = WallpaperManager.open(SAVE_DIRECTORY, file_out=sys.stdout)

    if args.index:
        for index in manager.indexed:
            print(index)
        exit(0)

    if args.histogram:
        histogram = histogram_extensions(manager.indexed)
        print(histogram)
        exit(0)

    if args.server:
        WallpaperManager.ezrun(manager, InputServer, sys.stdout)
    elif args.client:
        client = Client()
        while True:
            try:
                command = input(">> ")
            except KeyboardInterrupt:
                break
            client.send(command)
    elif args.csend:
        client = Client()
        client.send(args.csend)
    else:
        WallpaperManager.ezrun(manager, InputStdin, sys.stdout)

if __name__ == '__main__':
    main()
