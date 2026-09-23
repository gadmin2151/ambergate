"""Opt-in Linux network namespace connector for locally discovered containers.

Only a short-lived helper enters the target namespace. It returns a connected
socket over a private Unix socketpair; the agent and its TLS tunnel stay put.
Docker access is read-only and target PIDs/addresses never come from the center.
"""
import array
import ctypes
import ipaddress
import os
import re
import socket
import subprocess
import sys
import time

from .docker import DockerError


def enter_namespace(fd):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.setns(fd, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def exchange(namespace_fd, port=0, addresses=()):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    process, received = None, []
    try:
        parent.settimeout(6)
        process = subprocess.Popen([sys.executable, '-m', 'ambergate.agent_network',
            str(namespace_fd), str(child.fileno()), str(port), *addresses],
            pass_fds=(namespace_fd, child.fileno()), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1'})
        child.close()
        payload, ancillary, flags, _ = parent.recvmsg(256, socket.CMSG_SPACE(array.array('i').itemsize))
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                values = array.array('i'); values.frombytes(data[:len(data) - len(data) % values.itemsize])
                received.extend(values)
        process.wait(timeout=1)
        if payload != b'ok' or flags or process.returncode or len(received) != bool(port):
            raise OSError('Namespace connection failed; check the application port and agent permissions')
        if port:
            connection = socket.socket(fileno=received.pop())
            connection.set_inheritable(False)
            connection.settimeout(5)
            return connection
    finally:
        parent.close(); child.close()
        for fd in received:
            os.close(fd)
        if process and process.poll() is None:
            process.kill(); process.wait()


class NamespaceConnector:
    def __init__(self, docker):
        self.docker = docker
        self.identity = None

    def inspect(self, identity, own=False):
        pattern = r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}' if own else r'[a-f0-9]{64}'
        if not isinstance(identity, str) or not re.fullmatch(pattern, identity):
            raise ValueError('Invalid namespace container ID')
        path = self.docker.read()['socket_path']
        version = self.docker.get(path, '/version').get('ApiVersion', '')
        if not isinstance(version, str) or not re.fullmatch(r'1\.\d{1,3}', version):
            raise DockerError('Unsupported Docker API version')
        value = self.docker.get(path, '/v' + version + '/containers/' + identity + '/json')
        state = value.get('State', {})
        if (not re.fullmatch(r'[a-f0-9]{64}', str(value.get('Id', '')))
                or not own and value['Id'] != identity
                or state.get('Running') is not True
                or type(state.get('Pid')) is not int or state['Pid'] <= 0
                or not isinstance(state.get('StartedAt'), str) or not state['StartedAt']):
            raise DockerError('Namespace target is no longer running')
        return value

    def open_namespace(self, info):
        fd = os.open('/proc/' + str(info['State']['Pid']) + '/ns/net', os.O_RDONLY | os.O_CLOEXEC)
        try:
            # Pin the namespace, then recheck Docker identity to reject restarts
            # and PID reuse between inspection and opening /proc.
            current = self.inspect(info['Id'])
            if any(current['State'][k] != info['State'][k] for k in ('Pid', 'StartedAt')):
                raise DockerError('Namespace target changed during connection')
            return fd
        except BaseException:
            os.close(fd)
            raise

    def check(self):
        try:
            info = self.inspect(self.docker.read()['gateway_container'] or socket.gethostname(), own=True)
            self.identity = info['Id']
            fd = self.open_namespace(info)
            try:
                exchange(fd)
            finally:
                os.close(fd)
        except (OSError, ValueError, DockerError, subprocess.SubprocessError) as exc:
            raise ValueError('Namespace mode needs Linux Docker, --pid=host, --cap-add SYS_ADMIN and '
                             '--cap-add SYS_PTRACE; also check docker.sock and AMBERGATE_DOCKER_CONTAINER') from exc

    def connect(self, identity, port):
        if identity == self.identity:
            raise ValueError('The agent cannot target itself')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Invalid namespace port')
        info = self.inspect(identity)
        addresses = ['127.0.0.1', '::1']
        for network in info.get('NetworkSettings', {}).get('Networks', {}).values():
            for key in ('IPAddress', 'GlobalIPv6Address'):
                try:
                    address = ipaddress.ip_address(network.get(key, ''))
                    if not (address.is_unspecified or address.is_multicast or address.is_link_local):
                        addresses.append(str(address))
                except ValueError:
                    pass
        fd = self.open_namespace(info)
        try:
            return exchange(fd, port, list(dict.fromkeys(addresses))[:66])
        finally:
            os.close(fd)


def worker():
    namespace_fd, ipc_fd, port = map(int, sys.argv[1:4])
    ipc = socket.socket(fileno=ipc_fd)
    try:
        enter_namespace(namespace_fd)
        os.close(namespace_fd)
        # The helper needs no privileges once it has entered the namespace.
        os.setgroups([]); os.setgid(65534); os.setuid(65534)
        if not port:
            ipc.send(b'ok')
            return
        deadline = time.monotonic() + 5
        for candidate in sys.argv[4:]:
            if time.monotonic() >= deadline:
                break
            address = str(ipaddress.ip_address(candidate))
            try:
                with socket.create_connection((address, port), timeout=.5) as connection:
                    ipc.sendmsg([b'ok'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [connection.fileno()]))])
                    return
            except OSError:
                continue
        raise OSError('Application port unavailable')
    except (OSError, ValueError):
        ipc.send(b'failed')
        raise SystemExit(1) from None
    finally:
        ipc.close()


if __name__ == '__main__':
    worker()
