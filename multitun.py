#!/usr/bin/env python2

# multitun v0.10
#
# Joshua Davis (multitun -*- covert.codes)
# http://covert.codes
# Copyright(C) 2014
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import dpkt
import logging
import struct
import sys
from ast import literal_eval
from autobahn.twisted.websocket import WebSocketServerFactory
from autobahn.twisted.websocket import WebSocketServerProtocol
from autobahn.twisted.websocket import WebSocketClientFactory
from autobahn.twisted.websocket import WebSocketClientProtocol
from autobahn.twisted.resource import WebSocketResource
from iniparse import INIConfig
from socket import inet_ntoa, inet_aton
from twisted.internet import protocol, reactor
from twisted.web.server import Site
from twisted.web.static import File
from twisted.python import log
from mtcrypt.mtcrypt import *

try:
    import os
    import pywintypes
    import threading
    import win32api
    import win32event
    import win32file
    import _winreg as reg
    WINDOWS = True
    BSD = False
    LINUX = False
except:
    WINDOWS = False

    try:
        from pytun import TunTapDevice, IFF_TUN, IFF_NO_PI
        LINUX = True
        BSD = False
    except:
        import os
        from subprocess import call
        LINUX = False
        BSD = True


MT_VERSION= "v0.10"
CONF_FILE = "multitun.conf"
ERR = -1


class WSServerFactory(WebSocketServerFactory):
    """WebSocket client protocol callbacks"""

    def __init__(self, path, debug, debugCodePaths=False):
        WebSocketServerFactory.__init__(self, path, debug=debug, debugCodePaths=False)

        # Holds currently connected clients
        self.clients = dict()


    def tunnel_write(self, data):
        """Server: receive data from TUN, send to client"""
        taddr = inet_ntoa(dpkt.ip.IP(data)['dst'])
        try:
            dst_proto = self.clients[taddr]
        except:
            return

        try:
            dst_proto.tunnel_write(data)
        except:
            log.msg("Couldn't reach the client over the WebSocket.", logLevel=logging.INFO)


    def register(self, taddr, proto):
        # return False if desired TUN addr already in use
        if taddr in self.clients:
            return False

        self.clients[taddr] = proto


    def unregister(self, proto):
        for c in self.clients:
            if self.clients[c] == proto:
                self.clients.pop(c, None)
                break


class WSServerProto(WebSocketServerProtocol):
    """WebSocket server protocol callbacks"""

    def onConnect(self, response):
        log.msg("WebSocket connected", logLevel=logging.INFO)


    def onOpen(self):
        log.msg("WebSocket opened", logLevel=logging.INFO)
        self.mtcrypt = MTCrypt(is_server=True)
        self.factory.proto = self
        self.mtcrypt.proto = self


    def onClose(self, wasClean, code, reason):
        self.factory.unregister(self)
        log.msg("WebSocket closed", logLevel=logging.INFO)


    def onMessage(self, data, isBinary):
        """Get data from the server WebSocket, send to the TUN"""
        data = self.mtcrypt.decrypt(data)
        if data == None:
            return

        try:
            self.factory.tun.doWrite(data)
        except:
            log.msg("Error writing to TUN", logLevel=logging.INFO)


    def connectionLost(self, reason):
        WebSocketServerProtocol.connectionLost(self, reason)


    def tunnel_write(self, data):
        """Server: TUN sends data through WebSocket to client"""
        data = self.mtcrypt.encrypt(data)
        self.sendMessage(data, isBinary=True)


class WSClientFactory(WebSocketClientFactory):
    def __init__(self, path, debug, debugCodePaths=False):
        WebSocketClientFactory.__init__(self, path, debug=debug, debugCodePaths=False)


    def tunnel_write(self, data):
        """WS Client: Received data from TUN"""
        try:
            self.proto.tunnel_write(data)
        except:
            log.msg("Couldn't reach the server over the WebSocket", logLevel=logging.INFO)


class WSClientProto(WebSocketClientProtocol):
    """WS client: WebSocket client protocol callbacks"""

    def onConnect(self, response):
        log.msg("WebSocket connected", logLevel=logging.INFO)


    def onOpen(self):
        log.msg("WebSocket opened", logLevel=logging.INFO)
        self.mtcrypt = MTCrypt(is_server=False)
        self.factory.proto = self
        self.mtcrypt.proto = self
        

    def onClose(self, wasClean, code, reason):
        log.msg("WebSocket closed", logLevel=logging.INFO)
        

    def onMessage(self, data, isBinary):
        """Client: Received data from WS, decrypt and send to TUN"""
        data = self.mtcrypt.decrypt(data)
        if data == None:
            return

        try:
            self.factory.tun.doWrite(data)
        except:
            log.msg("Error writing to TUN", logLevel=logging.INFO)


    def tunnel_write(self, data):
        """Client: TUN sends data through WebSocket to server"""
        data = self.mtcrypt.encrypt(data)
        self.sendMessage(data, isBinary=True)


if WINDOWS == True:
    class TunRead(threading.Thread):
        '''Read from localhost, send toward server'''

        ETHERNET_MTU = 1500

        def __init__(self, tun_handle, wsfactory):
            self.tun_handle = tun_handle
            self.wsfactory = wsfactory
            self.goOn = True
            self.overlappedRx = pywintypes.OVERLAPPED()
            self.overlappedRx.hEvent = win32event.CreateEvent(None, 0, 0, None)

            threading.Thread.__init__(self)
            self.name = "tunRead"


        def run(self):
            rxbuffer = win32file.AllocateReadBuffer(self.ETHERNET_MTU)

            while self.goOn:
                l, data = win32file.ReadFile(self.tun_handle, rxbuffer, self.overlappedRx)
                win32event.WaitForSingleObject(self.overlappedRx.hEvent, win32event.INFINITE)
                self.overlappedRx.Offset = self.overlappedRx.Offset + len(data)
                self.wsfactory.tunnel_write(data)


        def close(self):
            self.goOn = False


class TUN(object):
    """TUN device"""

    def __init__(self, tun_dev, tun_addr, tun_remote_addr, tun_nm, tun_mtu, wsfactory):
        self.tun_dev = tun_dev
        self.tun_addr = tun_addr
        self.addr = tun_addr # used by mtcrypt
        self.tun_nm = tun_nm
        self.tun_mtu = int(tun_mtu)
        self.wsfactory = wsfactory

        if BSD == True:
            try:
                self.tunfd = os.open("/dev/"+tun_dev, os.O_RDWR)
                call(["/sbin/ifconfig", tun_dev, tun_addr, tun_remote_addr, "up"])
            except:
                log.msg("Error opening TUN device.  In use?  Permissions?", logLevel=logging.WARN)
                sys.exit(ERR)

            reactor.addReader(self)

            logstr = ("Opened TUN device on %s") % (self.tun_dev)
            log.msg(logstr, logLevel=logging.INFO)


        elif LINUX == True:
            try:
                self.tun = TunTapDevice(name=tun_dev, flags=(IFF_TUN|IFF_NO_PI))
            except:
                log.msg("Error opening TUN device.  In use?  Permissions?", logLevel=logging.WARN)
                sys.exit(ERR)

            self.tun.addr = tun_addr
            self.tun.dstaddr = tun_remote_addr
            self.tun.netmask = tun_nm
            self.tun.mtu = int(tun_mtu)
            self.tun.up()

            reactor.addReader(self)

            logstr = ("Opened TUN device on %s") % (self.tun.name)
            log.msg(logstr, logLevel=logging.INFO)

        elif WINDOWS == True:
            self.overlappedTx = pywintypes.OVERLAPPED()
            self.overlappedTx.hEvent = win32event.CreateEvent(None, 0, 0, None)
            
            addr_tmp = self.tun_addr.split('.')
            self.tun_ipv4_address = list()
            for i in range(0, 4):
                self.tun_ipv4_address.append(int(addr_tmp[i]))

            nm_tmp = self.tun_nm.split('.')
            self.tun_ipv4_netmask = list()
            self.tun_ipv4_network = list()
            for i in range(0, 4):
                self.tun_ipv4_netmask.append(int(nm_tmp[i]))
                self.tun_ipv4_network.append(int(addr_tmp[i]) & int(nm_tmp[i]))

            self.TAP_IOCTL_CONFIG_POINT_TO_POINT = self.TAP_CONTROL_CODE(5, 0)
            self.TAP_IOCTL_SET_MEDIA_STATUS = self.TAP_CONTROL_CODE(6, 0)
            self.TAP_IOCTL_CONFIG_TUN = self.TAP_CONTROL_CODE(10, 0)

            try:
                self.tun_handle = self.openTunTap()
            except:
                log.msg("Could not open TUN device.  Permissions?", logLevel=logging.WARN)
                sys.exit(ERR)

            log.msg("Opened TUN device", logLevel=logging.INFO)

            self.tunRead = TunRead(self.tun_handle, self.wsfactory)
            self.tunRead.start()


    if WINDOWS == True:
        def doWrite(self, data):
            win32file.WriteFile(self.tun_handle, data, self.overlappedTx)
            win32event.WaitForSingleObject(self.overlappedTx.hEvent, win32event.INFINITE)
            self.overlappedTx.Offset = self.overlappedTx.Offset + len(data)

        def openTunTap(self):
            guid = self.get_device_guid()

            tun_handle = win32file.CreateFile(
                r'\\.\Global\%s.tap' % guid,
                win32file.GENERIC_READ    | win32file.GENERIC_WRITE,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                None, win32file.OPEN_EXISTING,
                win32file.FILE_ATTRIBUTE_SYSTEM|win32file.FILE_FLAG_OVERLAPPED, None)

            win32file.DeviceIoControl(tun_handle,
                self.TAP_IOCTL_SET_MEDIA_STATUS,
                '\x01\x00\x00\x00',
                None)

            configTunParam  = []
            configTunParam += self.tun_ipv4_address
            configTunParam += self.tun_ipv4_network
            configTunParam += self.tun_ipv4_netmask
            configTunParam  = ''.join([chr(b) for b in configTunParam])

            win32file.DeviceIoControl(
                tun_handle,
                self.TAP_IOCTL_CONFIG_TUN,
                configTunParam,
                None)

            return tun_handle


        def get_device_guid(self):
            ADAPTER_KEY         = r'SYSTEM\CurrentControlSet\Control\Class\{4D36E972-E325-11CE-BFC1-08002BE10318}'
            TUNTAP_COMPONENT_ID = 'tap0901'

            with reg.OpenKey(reg.HKEY_LOCAL_MACHINE, ADAPTER_KEY) as adapters:
                try:
                    for i in range(10000):
                        key_name = reg.EnumKey(adapters, i)
                        with reg.OpenKey(adapters, key_name) as adapter:
                            try:
                                component_id = reg.QueryValueEx(adapter, 'ComponentId')[0]
                                if component_id == TUNTAP_COMPONENT_ID:
                                    return reg.QueryValueEx(adapter, 'NetCfgInstanceId')[0]
                            except WindowsError, err:
                                pass
                except WindowsError, err:
                    pass


        def CTL_CODE(self, device_type, function, method, access):
            return (device_type << 16) | (access << 14) | (function << 2) | method;


        def TAP_CONTROL_CODE(self, request, method):
            return self.CTL_CODE(34, request, method, 0)

    else: # not windows
        def fileno(self):
            if BSD == True:
                return self.tunfd
            else:
                return self.tun.fileno()


        def doRead(self):
            """Read from host, send to WS toward distant end"""
            if BSD == True:
                data = os.read(self.tunfd, self.tun_mtu)
            else:
                data = self.tun.read(self.tun.mtu)

            self.wsfactory.tunnel_write(data)


        def doWrite(self, data):
            if BSD == True:
                os.write(self.tunfd, data)
            else:
                self.tun.write(data)


    # all OS's
    def connectionLost(self, reason):
        log.msg("Connection lost", logLevel=logging.INFO)


    def logPrefix(self):
        return "MT TUN"


class Server(object):
    def __init__(self, serv_addr, serv_port, ws_loc, tun_dev, tun_addr, tun_client_addr, tun_nm, tun_mtu, webdir, users):
        # WebSocket
        path = "ws://"+serv_addr+":"+serv_port
        wsfactory = WSServerFactory(path, debug=False)
        wsfactory.protocol = WSServerProto
        wsfactory.users = users

        # Web server
        ws_resource = WebSocketResource(wsfactory)
        root = File(webdir)
        root.putChild(ws_loc, ws_resource)
        site = Site(root)

        # TUN device
        tun = TUN(tun_dev, tun_addr, tun_client_addr, tun_nm, tun_mtu, wsfactory)
        wsfactory.tun = tun
        if WINDOWS == False:
            reactor.addReader(tun)

        reactor.listenTCP(int(serv_port), site)
        reactor.run()


class Client(object):
    def __init__(self, serv_addr, serv_port, ws_loc, tun_dev, tun_addr, tun_serv_addr, tun_nm, tun_mtu, passwd):
        # WebSocket
        path = "ws://"+serv_addr+":"+serv_port+"/"+ws_loc
        wsfactory = WSClientFactory(path, debug=False)
        wsfactory.protocol = WSClientProto
        wsfactory.protocol.passwd = passwd

        # TUN device
        tun = TUN(tun_dev, tun_addr, tun_serv_addr, tun_nm, tun_mtu, wsfactory)
        wsfactory.tun = tun

        reactor.connectTCP(serv_addr, int(serv_port), wsfactory)
        reactor.run()


banner = """

                 | | | (_) |              
  _ __ ___  _   _| | |_ _| |_ _   _ _ __  
 | '_ ` _ \| | | | | __| | __| | | | '_ \ 
 | | | | | | |_| | | |_| | |_| |_| | | | |
 |_| |_| |_|\____|_|\__|_|\__|\____|_| |_|
"""

def main():
    server = False
    for arg in sys.argv:
        if arg == "-s":
            server = True

    print banner
    print " =============================================="
    print " Multitun " + MT_VERSION
    print " By Joshua Davis (multitun -*- covert.codes)"
    print " http://covert.codes"
    print " Copyright(C) 2014"
    print " Released under the GNU General Public License"
    print " =============================================="
    print ""

    config = INIConfig(open(CONF_FILE))

    serv_addr = config.all.serv_addr
    serv_port = config.all.serv_port
    ws_loc = config.all.ws_loc
    tun_nm = config.all.tun_nm
    tun_mtu = config.all.tun_mtu
    serv_tun_addr = config.all.serv_tun_addr

    log.startLogging(sys.stdout)
    if type(config.all.logfile) == type(str()):
        try:
            log.msg("Trying to open logfile for writing", logLevel=logging.INFO)
            log.startLogging(open(config.all.logfile, 'a'))
        except:
            log.msg("Couldn't open logfile.  Permissions?", logLevel=logging.INFO)

    if server == True:
        users = literal_eval(config.server.users)
        if len(users) == 0:
            log.msg("No users specified in configuration file", logLevel=logging.WARN)
            sys.exit(ERR)
 
        tun_dev = config.server.tun_dev
        tun_client_addr = config.server.p2paddr
        webdir = config.server.webdir

        logstr = ("Starting multitun as a server on port %s") % (serv_port)
        log.msg(logstr, logLevel=logging.INFO)

        server = Server(serv_addr, serv_port, ws_loc, tun_dev, serv_tun_addr, tun_client_addr, tun_nm, tun_mtu, webdir, users)

    else: # server != True
        passwd = config.client.password
        if len(passwd) == 0:
            log.msg("Edit the configuration file to include a password", logLevel=logging.WARN)
            sys.exit(ERR)

        tun_dev = config.client.tun_dev
        tun_addr = config.client.tun_addr
        serv_tun_addr = config.all.serv_tun_addr

        logstr = ("Starting as client, forwarding to %s:%s") % (serv_addr, int(serv_port))
        log.msg(logstr, logLevel=logging.INFO)

        client = Client(serv_addr, serv_port, ws_loc, tun_dev, tun_addr, serv_tun_addr, tun_nm, tun_mtu, passwd)


def win_exit(sig, func=None):
    os._exit(0)


if __name__ == "__main__":
    if WINDOWS == True:
        win32api.SetConsoleCtrlHandler(win_exit, True)

    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

