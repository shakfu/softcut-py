// Single-producer / single-consumer lock-free ring of small commands, shared by
// the Python extension (src/softcut/_core.cpp) and the standalone server
// (clients/softcut-osc). A producer thread pushes tiny lambdas; the audio thread
// drains them each block. Lambdas that fit std::function's small-buffer storage
// make push/pop/call heap-free.
//
// One queue has exactly one producer and one consumer. Where two independent
// producers exist (e.g. a Python control thread and a native OSC thread), give
// each its own queue and drain them from the single consumer, rather than
// sharing one queue -- that keeps every queue strictly SPSC.
#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <functional>

namespace scsh {

struct CommandQueue {
    static constexpr size_t CAP = 4096;  // power of two
    std::array<std::function<void()>, CAP> buf;
    std::atomic<size_t> head{0};  // producer writes here
    std::atomic<size_t> tail{0};  // consumer reads here

    bool push(const std::function<void()> &fn) {
        const size_t h = head.load(std::memory_order_relaxed);
        const size_t n = (h + 1) & (CAP - 1);
        if (n == tail.load(std::memory_order_acquire)) return false;  // full
        buf[h] = fn;
        head.store(n, std::memory_order_release);
        return true;
    }

    bool pop(std::function<void()> &out) {
        const size_t t = tail.load(std::memory_order_relaxed);
        if (t == head.load(std::memory_order_acquire)) return false;  // empty
        out = std::move(buf[t]);
        tail.store((t + 1) & (CAP - 1), std::memory_order_release);
        return true;
    }
};

}  // namespace scsh
