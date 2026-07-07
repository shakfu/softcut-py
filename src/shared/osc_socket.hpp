// Minimal cross-platform UDP socket helpers, shared by the Python extension's
// native OSC transport (src/softcut/_core.cpp, under SOFTCUT_TINYOSC) and the
// standalone server (clients/softcut-osc). IPv4 only -- just what the softcut OSC
// protocol needs. tinyosc supplies the wire codec; the transport is this glue.
#pragma once

#include <cstdint>
#include <cstring>
#include <string>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <sys/time.h>
#include <unistd.h>
#endif

namespace scsh {

#ifdef _WIN32
using socket_t = SOCKET;
static const socket_t kInvalidSocket = INVALID_SOCKET;
inline void close_socket(socket_t s) { closesocket(s); }
inline void ensure_sockets() {
    static bool done = false;
    if (!done) {
        WSADATA w;
        WSAStartup(MAKEWORD(2, 2), &w);
        done = true;
    }
}
inline void set_recv_timeout_ms(socket_t s, int ms) {
    DWORD tv = static_cast<DWORD>(ms);
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char *>(&tv),
               sizeof(tv));
}
#else
using socket_t = int;
static const socket_t kInvalidSocket = -1;
inline void close_socket(socket_t s) { ::close(s); }
inline void ensure_sockets() {}
inline void set_recv_timeout_ms(socket_t s, int ms) {
    struct timeval tv;
    tv.tv_sec = ms / 1000;
    tv.tv_usec = (ms % 1000) * 1000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
}
#endif

// Fill an IPv4 sockaddr_in; empty/"0.0.0.0"/unparseable host means INADDR_ANY.
inline void make_sockaddr(const std::string &host, int port, sockaddr_in &addr) {
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port));
    if (host.empty() || host == "0.0.0.0" ||
        inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) {
        addr.sin_addr.s_addr = INADDR_ANY;
    }
}

}  // namespace scsh
