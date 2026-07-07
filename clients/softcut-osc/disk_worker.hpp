// Background disk-worker thread. Buffer read/write/clear jobs are posted from the
// OSC thread and run here, off the realtime and OSC paths, so a slow file load
// never blocks OSC dispatch. Jobs run in FIFO order on one thread, so a write
// posted after a read observes that read's result. This mirrors norns'
// BufDiskWork; the click-avoidance crossfade lives in the job itself (see
// buffer_io.hpp apply_to_buffer).
#pragma once

#include <condition_variable>
#include <deque>
#include <functional>
#include <mutex>
#include <thread>

namespace scosc {

class DiskWorker {
public:
    DiskWorker() { thread_ = std::thread([this] { run(); }); }
    ~DiskWorker() { stop(); }

    // Post a job (a self-contained closure). Non-blocking.
    void post(std::function<void()> job) {
        {
            std::lock_guard<std::mutex> lk(mtx_);
            if (!running_) return;
            jobs_.push_back(std::move(job));
        }
        cv_.notify_one();
    }

    // Finish queued jobs, then stop and join. Draining on stop guarantees a
    // write posted just before shutdown completes (its file is flushed).
    void stop() {
        {
            std::lock_guard<std::mutex> lk(mtx_);
            if (!running_) return;
            running_ = false;
        }
        cv_.notify_all();
        if (thread_.joinable()) thread_.join();
    }

private:
    void run() {
        std::unique_lock<std::mutex> lk(mtx_);
        for (;;) {
            cv_.wait(lk, [this] { return !running_ || !jobs_.empty(); });
            while (!jobs_.empty()) {
                auto job = std::move(jobs_.front());
                jobs_.pop_front();
                lk.unlock();
                job();  // run disk I/O without holding the lock
                lk.lock();
            }
            if (!running_) return;  // stopped and fully drained
        }
    }

    std::deque<std::function<void()>> jobs_;
    std::mutex mtx_;
    std::condition_variable cv_;
    bool running_ = true;
    std::thread thread_;
};

}  // namespace scosc
