// Minimal D3D11 proxy for the Scrap Mechanic dedicated server.
//
// The game imports D3D11CreateDevice and D3D11CreateDeviceAndSwapChain.
// We forward those two entry points to the real system d3d11.dll and then
// replace the immediate-context draw/clear/dispatch methods and swap-chain
// Present with no-ops.  Resource creation and device state remain untouched,
// so the engine can still load data and run its simulation.
#include <windows.h>
#include <d3d11.h>
#include <dxgi.h>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>

namespace {

HMODULE original_module() {
    static HMODULE module = nullptr;
    static bool attempted = false;
    if (!attempted) {
        attempted = true;
        wchar_t system_dir[MAX_PATH] = {};
        UINT length = GetSystemDirectoryW(system_dir, MAX_PATH);
        if (length > 0 && length < MAX_PATH) {
            wchar_t path[MAX_PATH] = {};
            wsprintfW(path, L"%ls\\d3d11.dll", system_dir);
            module = LoadLibraryExW(path, nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
        }
    }
    return module;
}

void log_line(const char* text) {
    wchar_t exe[MAX_PATH] = {};
    if (!GetModuleFileNameW(nullptr, exe, MAX_PATH)) return;
    wchar_t* slash = wcsrchr(exe, L'\\');
    if (!slash) return;
    *slash = L'\0';
    wchar_t path[MAX_PATH] = {};
    wsprintfW(path, L"%ls\\nullrender.log", exe);
    HANDLE file = CreateFileW(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                              nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return;
    DWORD written = 0;
    WriteFile(file, text, static_cast<DWORD>(strlen(text)), &written, nullptr);
    WriteFile(file, "\r\n", 2, &written, nullptr);
    CloseHandle(file);
}

// Scrap Mechanic's ChatServer packet handler (0x140b387c0 in the tested
// build) trusts the 16-bit string lengths inside a chat packet.  A large chat
// message can therefore be copied past the handler's fixed stack buffers and
// crash or hang the server.  Drop oversized ChatServer packets before the
// handler starts parsing them.
//
// The patch is version dependent.  It is applied only when the original bytes
// at the patch site match the tested build; otherwise the game is left alone.
constexpr std::uintptr_t kChatPatchRva = 0xb39d8b;
constexpr std::uintptr_t kChatResumeRva = 0xb39d91;
constexpr std::uintptr_t kChatNotChatRva = 0xb39fb9;
constexpr std::uintptr_t kChatDropRva = 0xb3b783;
constexpr std::size_t kChatPatchSize = 6;
constexpr std::uint32_t kMaxChatPacketBytes = 2048;
static_assert(kMaxChatPacketBytes * 8 == 0x4000, "packet bit limit must fit the emitted immediate");

// The original instruction at the patch site.  The preceding `cmp dil, 0x11`
// is left in place, so the cave can use its flags to preserve the non-chat
// dispatch path.
const unsigned char kChatOriginalBytes[kChatPatchSize] = {
    0x0f, 0x85, 0x28, 0x02, 0x00, 0x00,  // jne 0x140b39fb9
};

void append_u32(unsigned char* out, std::size_t& offset, std::uint32_t value) {
    std::memcpy(out + offset, &value, sizeof(value));
    offset += sizeof(value);
}

bool fits_rel32(std::uintptr_t from, std::uintptr_t target) {
    const std::int64_t delta = static_cast<std::int64_t>(target) -
                               static_cast<std::int64_t>(from);
    return delta >= INT32_MIN && delta <= INT32_MAX;
}

void append_jmp_rel32(unsigned char* out, std::size_t& offset,
                      std::uintptr_t from, std::uintptr_t target) {
    out[offset++] = 0xe9;
    const std::int32_t rel = static_cast<std::int32_t>(
        target - (from + 5));
    append_u32(out, offset, static_cast<std::uint32_t>(rel));
}

void append_jcc_rel32(unsigned char* out, std::size_t& offset,
                      unsigned char opcode, std::uintptr_t from,
                      std::uintptr_t target) {
    out[offset++] = 0x0f;
    out[offset++] = opcode;
    const std::int32_t rel = static_cast<std::int32_t>(
        target - (from + 6));
    append_u32(out, offset, static_cast<std::uint32_t>(rel));
}

unsigned char* allocate_cave_near(std::uintptr_t patch_addr) {
    // A near allocation lets the patch use a 5-byte relative jump and keeps
    // the conditional branches in the cave within rel32 range.
    std::uintptr_t candidate =
        (patch_addr + 0x1000000 + 0xffff) & ~static_cast<std::uintptr_t>(0xffff);
    for (int attempt = 0; attempt < 128; ++attempt, candidate += 0x100000) {
        void* memory = VirtualAlloc(reinterpret_cast<void*>(candidate), 0x1000,
                                    MEM_COMMIT | MEM_RESERVE,
                                    PAGE_EXECUTE_READWRITE);
        if (memory != nullptr) return static_cast<unsigned char*>(memory);
    }
    return static_cast<unsigned char*>(
        VirtualAlloc(nullptr, 0x1000, MEM_COMMIT | MEM_RESERVE,
                     PAGE_EXECUTE_READWRITE));
}

void install_chat_limit() {
    static bool attempted = false;
    if (attempted) return;
    attempted = true;

    HMODULE exe = GetModuleHandleW(nullptr);
    if (!exe) {
        log_line("chat limit: main module not found");
        return;
    }
    auto* base = reinterpret_cast<unsigned char*>(exe);
    auto* patch_site = base + kChatPatchRva;

    if (std::memcmp(patch_site, kChatOriginalBytes, kChatPatchSize) != 0) {
        log_line("chat limit: patch site does not match this game build, skipped");
        return;
    }

    auto* cave = allocate_cave_near(reinterpret_cast<std::uintptr_t>(patch_site));
    if (!cave) {
        log_line("chat limit: executable cave allocation failed");
        return;
    }

    const auto patch_addr = reinterpret_cast<std::uintptr_t>(patch_site);
    const auto cave_addr = reinterpret_cast<std::uintptr_t>(cave);
    if (!fits_rel32(patch_addr + 5, cave_addr) ||
        !fits_rel32(cave_addr, reinterpret_cast<std::uintptr_t>(base + kChatNotChatRva)) ||
        !fits_rel32(cave_addr, reinterpret_cast<std::uintptr_t>(base + kChatDropRva)) ||
        !fits_rel32(cave_addr, reinterpret_cast<std::uintptr_t>(base + kChatResumeRva))) {
        log_line("chat limit: executable cave is out of range");
        VirtualFree(cave, 0, MEM_RELEASE);
        return;
    }

    std::size_t offset = 0;

    // Preserve the original non-chat branch.  The flags come from the
    // untouched `cmp dil, 0x11` immediately before this patch site.
    append_jcc_rel32(cave, offset, 0x85, cave_addr + offset,
                     reinterpret_cast<std::uintptr_t>(base + kChatNotChatRva));

    // [rsp+0x120] is the total packet size in bits.  2048 bytes is far above
    // a normal chat message while staying below the handler's 0x800 buffers.
    const unsigned char compare_total_bits[] = {
        0x81, 0xbc, 0x24, 0x20, 0x01, 0x00, 0x00, 0x00,  // cmp dword [rsp+0x120],
        0x40, 0x00, 0x00,                                // 0x4000
    };
    std::memcpy(cave + offset, compare_total_bits, sizeof(compare_total_bits));
    offset += sizeof(compare_total_bits);

    append_jcc_rel32(cave, offset, 0x87, cave_addr + offset,
                     reinterpret_cast<std::uintptr_t>(base + kChatDropRva));
    append_jmp_rel32(cave, offset, cave_addr + offset,
                     reinterpret_cast<std::uintptr_t>(base + kChatResumeRva));

    DWORD old = 0;
    if (!VirtualProtect(patch_site, kChatPatchSize, PAGE_EXECUTE_READWRITE, &old)) {
        log_line("chat limit: patch page is not writable");
        VirtualFree(cave, 0, MEM_RELEASE);
        return;
    }

    // Replace the original `jne` with a jump to the cave.  The remaining byte
    // is a NOP; the preceding `cmp` and the rest of the function are intact.
    patch_site[0] = 0xe9;
    std::int32_t rel = static_cast<std::int32_t>(cave_addr - (patch_addr + 5));
    std::memcpy(patch_site + 1, &rel, sizeof(rel));
    patch_site[5] = 0x90;

    DWORD ignored = 0;
    VirtualProtect(patch_site, kChatPatchSize, old, &ignored);
    FlushInstructionCache(GetCurrentProcess(), patch_site, kChatPatchSize);
    log_line("chat limit: ChatServer chat packets over 2048 bytes are dropped");
}

// Hard block for the native container transaction command that directly
// writes an item slot (setItem).  Normal movement uses the moveFromSlot /
// collectToSlot commands, so this guard is intentionally narrow.  The client
// cheat path in the wild uses setItem to materialize arbitrary items.
constexpr std::uintptr_t kContainerSetItemRva = 0x7b92b8;
constexpr std::uintptr_t kContainerTxCleanupRva = 0x7b9c44;
constexpr unsigned char kContainerSetItemOriginal[] = {
    0x48, 0x8b, 0x95, 0xb8, 0x00,
};

// ---------------------------------------------------------------------------
// Server chat bridge.
//
// The dedicated server has no console/RCON chat command.  The engine does
// expose ChatServer::broadcast(std::string), which is used for its own system
// messages.  Hook its entry to copy incoming chat into a file consumed by the
// panel, and let the panel enqueue lines in outbox.log which are broadcast from
// the game's Present thread.
// ---------------------------------------------------------------------------
constexpr std::uintptr_t kChatBroadcastRva = 0xae4e50;
constexpr std::uintptr_t kChatBroadcastPatchSize = 5;
constexpr std::uintptr_t kChatServerGlobalRva = 0x1a69570;
const unsigned char kChatBroadcastOriginal[kChatBroadcastPatchSize] = {
    0x48, 0x89, 0x5c, 0x24, 0x18,
};

CRITICAL_SECTION g_chat_lock;
bool g_chat_lock_ready = false;
bool g_chat_bridge_enabled = false;

void ensure_chat_lock() {
    if (!g_chat_lock_ready) {
        InitializeCriticalSection(&g_chat_lock);
        g_chat_lock_ready = true;
    }
}

constexpr const wchar_t* kChatDirectory =
    L"C:\\users\\steamuser\\AppData\\Roaming\\Axolot Games\\Scrap Mechanic\\User\\.sm-chat";

void chat_path(wchar_t* out, std::size_t count, const wchar_t* name) {
    wsprintfW(out, L"%ls\\%ls", kChatDirectory, name);
    (void)count;
}

void append_chat_file(const wchar_t* name, const char* text) {
    ensure_chat_lock();
    wchar_t path[MAX_PATH] = {};
    chat_path(path, MAX_PATH, name);
    EnterCriticalSection(&g_chat_lock);
    HANDLE file = CreateFileW(path, FILE_APPEND_DATA,
                              FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                              OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file != INVALID_HANDLE_VALUE) {
        DWORD written = 0;
        WriteFile(file, text, static_cast<DWORD>(std::strlen(text)), &written,
                  nullptr);
        WriteFile(file, "\r\n", 2, &written, nullptr);
        CloseHandle(file);
    }
    LeaveCriticalSection(&g_chat_lock);
}

struct MsvcString {
    char buffer[16];
    std::size_t length;
    std::size_t capacity;
};

bool string_from_msvc(void* raw, char* out, std::size_t out_size) {
    if (!raw || IsBadReadPtr(raw, sizeof(MsvcString))) return false;
    const auto* value = reinterpret_cast<const MsvcString*>(raw);
    if (value->length == 0 || value->length >= out_size) return false;
    const char* data = value->length <= 15
                           ? value->buffer
                           : *reinterpret_cast<const char* const*>(value->buffer);
    if (!data || IsBadReadPtr(data, value->length)) return false;
    std::memcpy(out, data, value->length);
    out[value->length] = '\0';
    for (std::size_t i = 0; i < value->length; ++i) {
        if (out[i] == '\r' || out[i] == '\n') out[i] = ' ';
    }
    return true;
}

void on_chat_broadcast(void* raw_string) {
    char text[1024] = {};
    if (!string_from_msvc(raw_string, text, sizeof(text))) return;
    if (text[0] == '#' || std::strstr(text, "CHAT_PLAYER_") != nullptr) return;

    SYSTEMTIME now = {};
    GetLocalTime(&now);
    char line[1200] = {};
    std::snprintf(line, sizeof(line), "[%02u:%02u:%02u] %s", now.wHour,
                  now.wMinute, now.wSecond, text);
    append_chat_file(L"inbox.log", line);
}

bool send_server_chat(const char* text) {
    HMODULE exe = GetModuleHandleW(nullptr);
    if (!exe) return false;
    auto* base = reinterpret_cast<unsigned char*>(exe);
    auto* server_slot = reinterpret_cast<void**>(base + kChatServerGlobalRva);
    void* server = *server_slot;
    if (!server || IsBadReadPtr(server, sizeof(void*))) return false;

    char copy[768] = {};
    std::strncpy(copy, text, sizeof(copy) - 1);
    std::size_t length = std::strlen(copy);
    MsvcString value = {};
    if (length <= 15) {
        std::memcpy(value.buffer, copy, length + 1);
    } else {
        char* allocated = static_cast<char*>(HeapAlloc(
            GetProcessHeap(), 0, length + 1));
        if (!allocated) return false;
        std::memcpy(allocated, copy, length + 1);
        *reinterpret_cast<char**>(value.buffer) = allocated;
    }
    value.length = length;
    value.capacity = length <= 15 ? 15 : length;
    using BroadcastFn = void (*)(void*, void*);
    auto function = reinterpret_cast<BroadcastFn>(base + kChatBroadcastRva);
    function(server, &value);
    if (length > 15) {
        HeapFree(GetProcessHeap(), 0, *reinterpret_cast<char**>(value.buffer));
    }
    return true;
}

void chat_poll() {
    if (!g_chat_bridge_enabled) return;
    static ULONGLONG last_poll = 0;
    ULONGLONG now = GetTickCount64();
    if (now - last_poll < 250) return;
    last_poll = now;

    wchar_t path[MAX_PATH] = {};
    chat_path(path, MAX_PATH, L"outbox.log");
    HANDLE file = CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
                              FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                              OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return;

    LARGE_INTEGER size = {};
    LARGE_INTEGER offset = {};
    if (GetFileSizeEx(file, &size)) {
        static LONGLONG read_offset = 0;
        if (size.QuadPart < read_offset) read_offset = 0;
        offset.QuadPart = read_offset;
        if (size.QuadPart == read_offset) {
            CloseHandle(file);
            return;
        }
        SetFilePointerEx(file, offset, nullptr, FILE_BEGIN);
        char buffer[4096] = {};
        DWORD got = 0;
        if (!ReadFile(file, buffer, sizeof(buffer) - 1, &got, nullptr)) {
            CloseHandle(file);
            return;
        }
        buffer[got] = '\0';
        read_offset += got;
        char* line = buffer;
        bool all_complete = got > 0 && buffer[got - 1] == '\n';
        while (line && *line) {
            char* end = std::strchr(line, '\n');
            if (!end) break;
            *end = '\0';
            char* start = line;
            while (*start == ' ' || *start == '\t') ++start;
            if (*start && !send_server_chat(start)) {
                log_line("chat bridge: broadcast skipped; ChatServer not ready");
            }
            line = end + 1;
        }
        if (all_complete) {
            LARGE_INTEGER zero = {};
            SetFilePointerEx(file, zero, nullptr, FILE_BEGIN);
            SetEndOfFile(file);
            read_offset = 0;
        }
    }
    CloseHandle(file);
}

void install_chat_bridge() {
    static bool attempted = false;
    if (attempted) return;
    attempted = true;

    HMODULE exe = GetModuleHandleW(nullptr);
    if (!exe) {
        log_line("chat bridge: main module not found");
        return;
    }
    auto* base = reinterpret_cast<unsigned char*>(exe);
    auto* patch_site = base + kChatBroadcastRva;
    if (std::memcmp(patch_site, kChatBroadcastOriginal,
                    kChatBroadcastPatchSize) != 0) {
        log_line("chat bridge: patch site does not match this game build, skipped");
        return;
    }
    CreateDirectoryW(kChatDirectory, nullptr);

    auto* cave = allocate_cave_near(reinterpret_cast<std::uintptr_t>(patch_site));
    if (!cave) {
        log_line("chat bridge: cave allocation failed");
        return;
    }
    const auto patch_addr = reinterpret_cast<std::uintptr_t>(patch_site);
    const auto cave_addr = reinterpret_cast<std::uintptr_t>(cave);
    if (!fits_rel32(patch_addr + 5, cave_addr)) {
        VirtualFree(cave, 0, MEM_RELEASE);
        log_line("chat bridge: cave out of range");
        return;
    }

    std::size_t off = 0;
    const auto emit = [&](const unsigned char* bytes, std::size_t n) {
        std::memcpy(cave + off, bytes, n);
        off += n;
    };
    const auto emit_mov_r11 = [&](std::uintptr_t value) {
        unsigned char bytes[10] = {0x49, 0xbb};
        std::memcpy(bytes + 2, &value, sizeof(value));
        emit(bytes, sizeof(bytes));
    };
    const unsigned char prologue[] = {
        0x9c, 0x50, 0x51, 0x52,
        0x41, 0x50, 0x41, 0x51, 0x41, 0x52, 0x41, 0x53,
        0x48, 0x83, 0xec, 0x28,
        0x48, 0x89, 0xd1,
    };
    emit(prologue, sizeof(prologue));
    emit_mov_r11(reinterpret_cast<std::uintptr_t>(&on_chat_broadcast));
    const unsigned char call_logger[] = {0x41, 0xff, 0xd3};
    emit(call_logger, sizeof(call_logger));
    const unsigned char epilogue[] = {
        0x48, 0x83, 0xc4, 0x28,
        0x41, 0x5b, 0x41, 0x5a, 0x41, 0x59, 0x41, 0x58,
        0x5a, 0x59, 0x58, 0x9d,
        0x48, 0x89, 0x5c, 0x24, 0x18,
    };
    emit(epilogue, sizeof(epilogue));
    emit_mov_r11(reinterpret_cast<std::uintptr_t>(base + kChatBroadcastRva + 5));
    const unsigned char jump_back[] = {0x41, 0xff, 0xe3};
    emit(jump_back, sizeof(jump_back));

    DWORD old = 0;
    if (!VirtualProtect(patch_site, kChatBroadcastPatchSize,
                        PAGE_EXECUTE_READWRITE, &old)) {
        VirtualFree(cave, 0, MEM_RELEASE);
        log_line("chat bridge: patch page is not writable");
        return;
    }
    patch_site[0] = 0xe9;
    const auto rel = static_cast<std::int32_t>(cave_addr - (patch_addr + 5));
    std::memcpy(patch_site + 1, &rel, sizeof(rel));
    DWORD ignored = 0;
    VirtualProtect(patch_site, kChatBroadcastPatchSize, old, &ignored);
    FlushInstructionCache(GetCurrentProcess(), patch_site,
                          kChatBroadcastPatchSize);
    g_chat_bridge_enabled = true;
    log_line("chat bridge: installed");
}

void install_container_setitem_block() {
    log_line("container guard: init");
    static bool attempted = false;
    if (attempted) return;
    attempted = true;

    HMODULE exe = GetModuleHandleW(nullptr);
    if (!exe) {
        log_line("container guard: main module not found");
        return;
    }
    auto* base = reinterpret_cast<unsigned char*>(exe);
    auto* patch_site = base + kContainerSetItemRva;
    if (std::memcmp(patch_site, kContainerSetItemOriginal,
                    sizeof(kContainerSetItemOriginal)) != 0) {
        log_line("container guard: setItem site does not match this build, skipped");
        return;
    }

    const auto source = reinterpret_cast<std::uintptr_t>(patch_site);
    const auto target = reinterpret_cast<std::uintptr_t>(base + kContainerTxCleanupRva);
    const auto delta = static_cast<std::int64_t>(target) -
                       static_cast<std::int64_t>(source + 5);
    if (delta < INT32_MIN || delta > INT32_MAX) {
        log_line("container guard: setItem jump is out of range, skipped");
        return;
    }

    DWORD old = 0;
    if (!VirtualProtect(patch_site, sizeof(kContainerSetItemOriginal),
                        PAGE_EXECUTE_READWRITE, &old)) {
        log_line("container guard: setItem page is not writable");
        return;
    }
    patch_site[0] = 0xe9;
    const auto rel = static_cast<std::int32_t>(delta);
    std::memcpy(patch_site + 1, &rel, sizeof(rel));
    DWORD ignored = 0;
    VirtualProtect(patch_site, sizeof(kContainerSetItemOriginal), old, &ignored);
    FlushInstructionCache(GetCurrentProcess(), patch_site,
                          sizeof(kContainerSetItemOriginal));
    log_line("container guard: setItem transactions are dropped");
}

// Creative/limited container collect branch.  The transaction dispatcher uses
// Container2 flag 0xee to select the alternate collect path; blocking that
// branch stops items being pulled out of the client-side creative container
// while leaving the normal collect_core path untouched.
constexpr std::uintptr_t kCreativeCollectCallRva = 0x7b9559;
constexpr std::uintptr_t kCreativeCollectCleanupRva = 0x7b9c44;
constexpr unsigned char kCreativeCollectOriginal[] = {
    0xe8, 0x42, 0x15, 0x00, 0x00,
};

void install_creative_collect_block() {
    static bool attempted = false;
    if (attempted) return;
    attempted = true;

    HMODULE exe = GetModuleHandleW(nullptr);
    if (!exe) {
        log_line("container guard: creative collect module not found");
        return;
    }
    auto* base = reinterpret_cast<unsigned char*>(exe);
    auto* patch_site = base + kCreativeCollectCallRva;
    if (std::memcmp(patch_site, kCreativeCollectOriginal,
                    sizeof(kCreativeCollectOriginal)) != 0) {
        log_line("container guard: creative collect site mismatch, skipped");
        return;
    }

    const auto source = reinterpret_cast<std::uintptr_t>(patch_site);
    const auto target = reinterpret_cast<std::uintptr_t>(base + kCreativeCollectCleanupRva);
    const auto delta = static_cast<std::int64_t>(target) -
                       static_cast<std::int64_t>(source + 5);
    if (delta < INT32_MIN || delta > INT32_MAX) {
        log_line("container guard: creative collect jump out of range, skipped");
        return;
    }

    DWORD old = 0;
    if (!VirtualProtect(patch_site, sizeof(kCreativeCollectOriginal),
                        PAGE_EXECUTE_READWRITE, &old)) {
        log_line("container guard: creative collect page is not writable");
        return;
    }
    patch_site[0] = 0xe9;
    const auto rel = static_cast<std::int32_t>(delta);
    std::memcpy(patch_site + 1, &rel, sizeof(rel));
    DWORD ignored = 0;
    VirtualProtect(patch_site, sizeof(kCreativeCollectOriginal), old, &ignored);
    FlushInstructionCache(GetCurrentProcess(), patch_site,
                          sizeof(kCreativeCollectOriginal));
    log_line("container guard: creative collect transactions are dropped");
}

// When Container2 flag 0xee is clear, the transaction dispatcher takes the
// unlimited/creative collect path.  Limited (normal survival) containers use
// the alternate collect_alt branch.  Drop only the unlimited branch so normal
// hotbar/inventory movement keeps working.
constexpr std::uintptr_t kUnlimitedCollectCallRva = 0x7b9563;
constexpr std::uintptr_t kUnlimitedCollectCleanupRva = 0x7b9c44;
constexpr unsigned char kUnlimitedCollectOriginal[] = {
    0xe8, 0x98, 0x18, 0x00, 0x00,
};

void install_unlimited_collect_block() {
    static bool attempted = false;
    if (attempted) return;
    attempted = true;

    HMODULE exe = GetModuleHandleW(nullptr);
    if (!exe) {
        log_line("container guard: unlimited collect module not found");
        return;
    }
    auto* base = reinterpret_cast<unsigned char*>(exe);
    auto* patch_site = base + kUnlimitedCollectCallRva;
    if (std::memcmp(patch_site, kUnlimitedCollectOriginal,
                    sizeof(kUnlimitedCollectOriginal)) != 0) {
        log_line("container guard: unlimited collect site mismatch, skipped");
        return;
    }

    const auto source = reinterpret_cast<std::uintptr_t>(patch_site);
    const auto target = reinterpret_cast<std::uintptr_t>(base + kUnlimitedCollectCleanupRva);
    const auto delta = static_cast<std::int64_t>(target) -
                       static_cast<std::int64_t>(source + 5);
    if (delta < INT32_MIN || delta > INT32_MAX) {
        log_line("container guard: unlimited collect jump out of range, skipped");
        return;
    }

    DWORD old = 0;
    if (!VirtualProtect(patch_site, sizeof(kUnlimitedCollectOriginal),
                        PAGE_EXECUTE_READWRITE, &old)) {
        log_line("container guard: unlimited collect page is not writable");
        return;
    }
    patch_site[0] = 0xe9;
    const auto rel = static_cast<std::int32_t>(delta);
    std::memcpy(patch_site + 1, &rel, sizeof(rel));
    DWORD ignored = 0;
    VirtualProtect(patch_site, sizeof(kUnlimitedCollectOriginal), old, &ignored);
    FlushInstructionCache(GetCurrentProcess(), patch_site,
                          sizeof(kUnlimitedCollectOriginal));
    log_line("container guard: unlimited collect transactions are dropped");
}

static std::uint8_t safe_u8(std::uintptr_t address) {
    if (!address) return 0;
    const auto* pointer = reinterpret_cast<const void*>(address);
    if (IsBadReadPtr(pointer, 1)) return 0;
    return *reinterpret_cast<const std::uint8_t*>(pointer);
}

static std::uintptr_t safe_qword(std::uintptr_t address) {
    if (!address) return 0;
    const auto* pointer = reinterpret_cast<const void*>(address);
    if (IsBadReadPtr(pointer, sizeof(std::uintptr_t))) return 0;
    return *reinterpret_cast<const std::uintptr_t*>(pointer);
}

void log_move_probe(std::uintptr_t source, std::uintptr_t dest) {
    static int count = 0;
    if (count++ >= 80) return;
    const auto source_obj = safe_qword(source);
    const auto dest_obj = safe_qword(dest);
    char line[320] = {};
    std::snprintf(line, sizeof(line),
                  "moveprobe src=%016llx src_obj=%016llx src_flags=%02x/%02x "
                  "dst=%016llx dst_obj=%016llx dst_flags=%02x/%02x tid=%lu",
                  static_cast<unsigned long long>(source),
                  static_cast<unsigned long long>(source_obj),
                  safe_u8(source + 0xee), safe_u8(source_obj + 0xee),
                  static_cast<unsigned long long>(dest),
                  static_cast<unsigned long long>(dest_obj),
                  safe_u8(dest + 0xee), safe_u8(dest_obj + 0xee),
                  static_cast<unsigned long>(GetCurrentThreadId()));
    log_line(line);
}

// Cursor moves use the two-container moveFromSlot transaction.  Reject the
// move when either side is the unlimited/creative container (Container2 flag
// 0xee == 0).  Limited containers use flag 0xee != 0 and keep normal movement.
constexpr std::uintptr_t kContainerMoveCallRva = 0x7b94bd;
constexpr std::uintptr_t kContainerMoveResumeRva = 0x7b94c2;
constexpr std::uintptr_t kContainerMoveCleanupRva = 0x7b9c44;
constexpr std::uintptr_t kContainerMoveTargetRva = 0x7ba3c0;
constexpr unsigned char kContainerMoveOriginal[] = {
    0xe8, 0xfe, 0x0e, 0x00, 0x00,
};

void install_container_move_block() {
    static bool attempted = false;
    if (attempted) return;
    attempted = true;

    HMODULE exe = GetModuleHandleW(nullptr);
    if (!exe) {
        log_line("container guard: move module not found");
        return;
    }
    auto* base = reinterpret_cast<unsigned char*>(exe);
    auto* patch_site = base + kContainerMoveCallRva;
    if (std::memcmp(patch_site, kContainerMoveOriginal,
                    sizeof(kContainerMoveOriginal)) != 0) {
        log_line("container guard: move site mismatch, skipped");
        return;
    }

    auto* cave = allocate_cave_near(reinterpret_cast<std::uintptr_t>(patch_site));
    if (!cave) {
        log_line("container guard: move cave allocation failed");
        return;
    }
    const auto patch_addr = reinterpret_cast<std::uintptr_t>(patch_site);
    const auto cave_addr = reinterpret_cast<std::uintptr_t>(cave);
    if (!fits_rel32(patch_addr + 5, cave_addr)) {
        VirtualFree(cave, 0, MEM_RELEASE);
        log_line("container guard: move cave out of range");
        return;
    }

    std::size_t off = 0;
    const auto emit = [&](const unsigned char* bytes, std::size_t n) {
        std::memcpy(cave + off, bytes, n);
        off += n;
    };
    const auto emit_mov_r11 = [&](std::uintptr_t value) {
        unsigned char bytes[10] = {0x49, 0xbb};
        std::memcpy(bytes + 2, &value, sizeof(value));
        emit(bytes, sizeof(bytes));
    };

    // Log a bounded sample of move transactions before the guard.  This is
    // diagnostic only; it lets us distinguish an unlimited source from an
    // already-materialized normal inventory item without guessing.
    const unsigned char probe_prologue[] = {
        0x49, 0x89, 0xe3,                    // mov r11,rsp
        0x9c,                                // pushfq
        0x50, 0x51, 0x52,                    // push rax, rcx, rdx
        0x41, 0x50, 0x41, 0x51, 0x41, 0x52, 0x41, 0x53, // push r8,r9,r10,r11
        0x48, 0x83, 0xec, 0x28,              // sub rsp,0x28
        0x49, 0x8b, 0x4b, 0x50,              // mov rcx,[r11+0x50]
        0x49, 0x8b, 0x53, 0x68,              // mov rdx,[r11+0x68]
    };
    emit(probe_prologue, sizeof(probe_prologue));
    emit_mov_r11(reinterpret_cast<std::uintptr_t>(&log_move_probe));
    const unsigned char call_probe[] = {0x41, 0xff, 0xd3};
    emit(call_probe, sizeof(call_probe));
    const unsigned char probe_epilogue[] = {
        0x48, 0x83, 0xc4, 0x28,              // add rsp,0x28
        0x41, 0x5b, 0x41, 0x5a, 0x41, 0x59, 0x41, 0x58,
        0x5a, 0x59, 0x58, 0x9d,              // pops + popfq
    };
    emit(probe_epilogue, sizeof(probe_epilogue));

    const unsigned char check_source[] = {
        0x48, 0x8b, 0x44, 0x24, 0x50,        // mov rax,[rsp+0x50]
        0x48, 0x85, 0xc0,                    // test rax,rax
        0x74, 0x18,                          // je 0x22 (dest)
        0x80, 0xb8, 0xee, 0x00, 0x00, 0x00, 0x00, // cmp byte [rax+0xee],0
        0x74, 0x2d,                          // je 0x40 (drop)
    };
    const unsigned char check_dest[] = {
        0x48, 0x8b, 0x44, 0x24, 0x68,        // mov rax,[rsp+0x68]
        0x48, 0x85, 0xc0,                    // test rax,rax
        0x74, 0x09,                          // je 0x26 (original)
        0x80, 0xb8, 0xee, 0x00, 0x00, 0x00, 0x00,
        0x74, 0x1a,                          // je 0x40 (drop)
    };
    emit(check_source, sizeof(check_source));
    emit(check_dest, sizeof(check_dest));

    // original: call the move helper, then resume after the patched call
    emit_mov_r11(reinterpret_cast<std::uintptr_t>(base + kContainerMoveTargetRva));
    const unsigned char call_r11[] = {0x41, 0xff, 0xd3};
    emit(call_r11, sizeof(call_r11));
    emit_mov_r11(reinterpret_cast<std::uintptr_t>(base + kContainerMoveResumeRva));
    const unsigned char jmp_r11[] = {0x41, 0xff, 0xe3};
    emit(jmp_r11, sizeof(jmp_r11));
    // drop target
    emit_mov_r11(reinterpret_cast<std::uintptr_t>(base + kContainerMoveCleanupRva));
    emit(jmp_r11, sizeof(jmp_r11));

    DWORD old = 0;
    if (!VirtualProtect(patch_site, sizeof(kContainerMoveOriginal),
                        PAGE_EXECUTE_READWRITE, &old)) {
        VirtualFree(cave, 0, MEM_RELEASE);
        log_line("container guard: move page is not writable");
        return;
    }
    patch_site[0] = 0xe9;
    const auto rel = static_cast<std::int32_t>(cave_addr - (patch_addr + 5));
    std::memcpy(patch_site + 1, &rel, sizeof(rel));
    DWORD ignored = 0;
    VirtualProtect(patch_site, sizeof(kContainerMoveOriginal), old, &ignored);
    FlushInstructionCache(GetCurrentProcess(), patch_site,
                          sizeof(kContainerMoveOriginal));
    log_line("container guard: unlimited move transactions are dropped");
}

// Native probes for the container/inventory and shape-creation paths.  These
// are intentionally logging-only: they let us identify the exact server path
// used by a cheating client before we add a hard rejection rule.
struct ProbeSpec {
    const char* name;
    std::uintptr_t rva;
    const unsigned char* original;
    std::size_t size;
    bool block_creative;
};

constexpr unsigned char kShapeCreateOriginal[] = {
    0x48, 0x89, 0x5c, 0x24, 0x18,
};
constexpr unsigned char kContainerApplyOriginal[] = {
    0x48, 0x89, 0x5c, 0x24, 0x18,
};
constexpr unsigned char kCollectCoreOriginal[] = {
    0x48, 0x8b, 0xc4, 0x4c, 0x89, 0x40, 0x18,
};
constexpr unsigned char kCollectAltOriginal[] = {
    0x48, 0x8b, 0xc4, 0x44, 0x89, 0x48, 0x20,
};
constexpr unsigned char kCollectSlotOriginal[] = {
    0x40, 0x55, 0x56, 0x41, 0x56, 0x41, 0x57,
};
constexpr unsigned char kSpendOriginal[] = {
    0x48, 0x89, 0x54, 0x24, 0x10,
};

constexpr ProbeSpec kProbes[] = {
    {"container_apply", 0x7b91c0, kContainerApplyOriginal, sizeof(kContainerApplyOriginal), false},
    {"shape_create", 0xb3fcb0, kShapeCreateOriginal, sizeof(kShapeCreateOriginal), false},
    {"collect_core", 0x7bae00, kCollectCoreOriginal, sizeof(kCollectCoreOriginal), false},
    {"collect_alt", 0x7baaa0, kCollectAltOriginal, sizeof(kCollectAltOriginal), false},
    {"collect_slot", 0x7ba620, kCollectSlotOriginal, sizeof(kCollectSlotOriginal), false},
    {"spend", 0x7bb1e0, kSpendOriginal, sizeof(kSpendOriginal), false},
};

void probe_log_qwords(const char* name, const char* label, std::uint64_t address) {
    if (address == 0) {
        return;
    }
    const auto* pointer = reinterpret_cast<const void*>(address);
    if (IsBadReadPtr(pointer, sizeof(std::uint64_t) * 4)) {
        return;
    }
    const auto* words = static_cast<const std::uint64_t*>(pointer);
    char line[256] = {};
    std::snprintf(line, sizeof(line),
                  "anticheat mem %s %s=%016llx [%016llx %016llx %016llx %016llx] tid=%lu",
                  name, label,
                  static_cast<unsigned long long>(address),
                  static_cast<unsigned long long>(words[0]),
                  static_cast<unsigned long long>(words[1]),
                  static_cast<unsigned long long>(words[2]),
                  static_cast<unsigned long long>(words[3]),
                  static_cast<unsigned long>(GetCurrentThreadId()));
    log_line(line);
}

void probe_log_container_apply(std::uint64_t transaction) {
    if (IsBadReadPtr(reinterpret_cast<const void*>(transaction), 0x28)) {
        log_line("anticheat tx: unreadable transaction");
        return;
    }
    const auto* words = reinterpret_cast<const std::uint64_t*>(transaction);
    const auto capacity = words[2];
    const auto head = words[3];
    const auto count = words[4];
    const auto* entries = reinterpret_cast<const std::uint64_t*>(words[1]);
    char line[192] = {};
    std::snprintf(line, sizeof(line),
                  "anticheat tx count=%llu head=%llu cap=%llu entries=%016llx tid=%lu",
                  static_cast<unsigned long long>(count),
                  static_cast<unsigned long long>(head),
                  static_cast<unsigned long long>(capacity),
                  static_cast<unsigned long long>(words[1]),
                  static_cast<unsigned long>(GetCurrentThreadId()));
    log_line(line);
    if (count == 0 || capacity == 0 || entries == nullptr ||
        IsBadReadPtr(entries, sizeof(std::uint64_t) * 8)) {
        return;
    }
    const std::uint64_t limit = count < 8 ? count : 8;
    for (std::uint64_t i = 0; i < limit; ++i) {
        const std::uint64_t index = (head + i) & (capacity - 1);
        const auto* entry = reinterpret_cast<const std::uint64_t*>(entries[index]);
        if (entry == nullptr || IsBadReadPtr(entry, 0x40)) {
            break;
        }
        std::snprintf(line, sizeof(line),
                      "anticheat tx[%llu] type=%02llx q0=%016llx q1=%016llx q2=%016llx q3=%016llx q4=%016llx q5=%016llx q6=%016llx q7=%016llx",
                      static_cast<unsigned long long>(i),
                      static_cast<unsigned long long>(entry[0] & 0xff),
                      static_cast<unsigned long long>(entry[0]),
                      static_cast<unsigned long long>(entry[1]),
                      static_cast<unsigned long long>(entry[2]),
                      static_cast<unsigned long long>(entry[3]),
                      static_cast<unsigned long long>(entry[4]),
                      static_cast<unsigned long long>(entry[5]),
                      static_cast<unsigned long long>(entry[6]),
                      static_cast<unsigned long long>(entry[7]));
        log_line(line);
    }
}

void probe_log(const char* name, std::uint64_t rcx, std::uint64_t rdx,
               std::uint64_t r8, std::uint64_t r9, std::uint64_t ret) {
    if (std::strcmp(name, "container_apply") == 0) {
        static int count = 0;
        if (count++ >= 50) {
            return;
        }
    }
    char line[288] = {};
    std::snprintf(line, sizeof(line),
                  "anticheat probe %s rcx=%016llx rdx=%016llx r8=%016llx r9=%016llx ret=%016llx tid=%lu",
                  name,
                  static_cast<unsigned long long>(rcx),
                  static_cast<unsigned long long>(rdx),
                  static_cast<unsigned long long>(r8),
                  static_cast<unsigned long long>(r9),
                  static_cast<unsigned long long>(ret),
                  static_cast<unsigned long>(GetCurrentThreadId()));
    log_line(line);
    probe_log_qwords(name, "rdx", rdx);
    probe_log_qwords(name, "r8", r8);
    if (std::strcmp(name, "container_apply") == 0) {
        probe_log_container_apply(rdx);
    }
}

void append_mov_imm64(unsigned char* out, std::size_t& offset,
                      unsigned char rex, unsigned char opcode,
                      std::uintptr_t value) {
    out[offset++] = rex;
    out[offset++] = opcode;
    std::memcpy(out + offset, &value, sizeof(value));
    offset += sizeof(value);
}

bool install_probe(const ProbeSpec& spec, unsigned char* base) {
    auto* patch_site = base + spec.rva;
    if (std::memcmp(patch_site, spec.original, spec.size) != 0) {
        char line[160] = {};
        std::snprintf(line, sizeof(line),
                      "anticheat probe %s: bytes do not match this build, skipped",
                      spec.name);
        log_line(line);
        return false;
    }

    auto* cave = allocate_cave_near(reinterpret_cast<std::uintptr_t>(patch_site));
    if (!cave) {
        log_line("anticheat probe: executable cave allocation failed");
        return false;
    }

    const auto patch_addr = reinterpret_cast<std::uintptr_t>(patch_site);
    const auto cave_addr = reinterpret_cast<std::uintptr_t>(cave);
    if (!fits_rel32(patch_addr + 5, cave_addr)) {
        log_line("anticheat probe: cave is out of rel32 range");
        VirtualFree(cave, 0, MEM_RELEASE);
        return false;
    }

    std::size_t offset = 0;

    if (spec.block_creative) {
        // At entry rdx points at the Container2.  Drop collect operations whose
        // container is the client-side unlimited/creative container (flag 0).
        const unsigned char creative_check[] = {
            0x48, 0x85, 0xd2,                    // test rdx,rdx
            0x74, 0x15,                          // je continue
            0x48, 0x8b, 0x02,                    // mov rax,[rdx] ; Container2*
            0x48, 0x85, 0xc0,                    // test rax,rax
            0x74, 0x0d,                          // je continue
            0x80, 0xb8, 0xee, 0x00, 0x00, 0x00, 0x00, // cmp byte [rax+0xee],0
            0x75, 0x03,                          // jne continue
            0x31, 0xc0,                          // xor eax,eax
            0xc3,                                // ret
        };
        std::memcpy(cave + offset, creative_check, sizeof(creative_check));
        offset += sizeof(creative_check);
    }

    // Preserve the integer argument registers and flags around the logger.
    // The target function has not saved its own prologue registers yet.
    const unsigned char save_regs[] = {
        0x4c, 0x8b, 0x1c, 0x24,  // mov r11, [rsp] ; caller return address
        0x9c,                    // pushfq
        0x50,                    // push rax
        0x51,                    // push rcx
        0x52,                    // push rdx
        0x41, 0x50,              // push r8
        0x41, 0x51,              // push r9
        0x41, 0x52,              // push r10
        0x41, 0x53,              // push r11
        0x48, 0x83, 0xec, 0x38,  // sub rsp, 0x38 (32B shadow + 2 stack args)
    };
    std::memcpy(cave + offset, save_regs, sizeof(save_regs));
    offset += sizeof(save_regs);

    append_mov_imm64(cave, offset, 0x48, 0xb9,
                     reinterpret_cast<std::uintptr_t>(spec.name));
    const unsigned char args[] = {
        0x48, 0x8b, 0x54, 0x24, 0x60,        // rdx = saved rcx
        0x4c, 0x8b, 0x44, 0x24, 0x58,        // r8  = saved rdx
        0x4c, 0x8b, 0x4c, 0x24, 0x50,        // r9  = saved r8
        0x4c, 0x8b, 0x5c, 0x24, 0x48,        // r11 = saved r9
        0x4c, 0x89, 0x5c, 0x24, 0x20,        // 5th arg = original r9
        0x48, 0x8b, 0x44, 0x24, 0x38,        // rax = saved r11 (return address)
        0x48, 0x89, 0x44, 0x24, 0x28,        // 6th arg = return address
    };
    std::memcpy(cave + offset, args, sizeof(args));
    offset += sizeof(args);

    const auto logger = reinterpret_cast<std::uintptr_t>(&probe_log);
    append_mov_imm64(cave, offset, 0x49, 0xbb, logger);  // mov r11, logger
    const unsigned char call_r11[] = {0x41, 0xff, 0xd3};
    std::memcpy(cave + offset, call_r11, sizeof(call_r11));
    offset += sizeof(call_r11);

    const unsigned char restore_regs[] = {
        0x48, 0x83, 0xc4, 0x38,  // add rsp, 0x38
        0x41, 0x5b,              // pop r11
        0x41, 0x5a,              // pop r10
        0x41, 0x59,              // pop r9
        0x41, 0x58,              // pop r8
        0x5a,                    // pop rdx
        0x59,                    // pop rcx
        0x58,                    // pop rax
        0x9d,                    // popfq
    };
    std::memcpy(cave + offset, restore_regs, sizeof(restore_regs));
    offset += sizeof(restore_regs);

    std::memcpy(cave + offset, spec.original, spec.size);
    offset += spec.size;

    const auto resume = patch_addr + spec.size;
    append_mov_imm64(cave, offset, 0x49, 0xbb, resume);
    const unsigned char jmp_r11[] = {0x41, 0xff, 0xe3};
    std::memcpy(cave + offset, jmp_r11, sizeof(jmp_r11));
    offset += sizeof(jmp_r11);

    DWORD old = 0;
    if (!VirtualProtect(patch_site, spec.size, PAGE_EXECUTE_READWRITE, &old)) {
        log_line("anticheat probe: patch page is not writable");
        VirtualFree(cave, 0, MEM_RELEASE);
        return false;
    }

    patch_site[0] = 0xe9;
    const std::int32_t rel = static_cast<std::int32_t>(cave_addr - (patch_addr + 5));
    std::memcpy(patch_site + 1, &rel, sizeof(rel));
    for (std::size_t i = 5; i < spec.size; ++i) patch_site[i] = 0x90;

    DWORD ignored = 0;
    VirtualProtect(patch_site, spec.size, old, &ignored);
    FlushInstructionCache(GetCurrentProcess(), patch_site, spec.size);

    char line[160] = {};
    std::snprintf(line, sizeof(line), "anticheat probe %s: installed", spec.name);
    log_line(line);
    return true;
}

void install_probes() {
    install_container_setitem_block();
    static bool attempted = false;
    if (attempted) return;
    attempted = true;

    HMODULE exe = GetModuleHandleW(nullptr);
    if (!exe) {
        log_line("anticheat probe: main module not found");
        return;
    }
    auto* base = reinterpret_cast<unsigned char*>(exe);
    for (const auto& spec : kProbes) install_probe(spec, base);
}

bool patch_pointer(void** table, std::size_t index, void* value) {
    if (table == nullptr) return false;
    DWORD old = 0;
    if (!VirtualProtect(&table[index], sizeof(void*), PAGE_READWRITE, &old)) return false;
    table[index] = value;
    DWORD ignored = 0;
    VirtualProtect(&table[index], sizeof(void*), old, &ignored);
    return true;
}

void STDMETHODCALLTYPE nop_draw() {}
void STDMETHODCALLTYPE nop_clear() {}

HRESULT STDMETHODCALLTYPE nop_present(IDXGISwapChain*, UINT, UINT) {
    chat_poll();
    return S_OK;
}

void patch_context(ID3D11DeviceContext* context) {
    if (!context) return;
    void** table = *reinterpret_cast<void***>(context);
    if (!table) return;

    patch_pointer(table, 13, reinterpret_cast<void*>(&nop_draw));
    patch_pointer(table, 12, reinterpret_cast<void*>(&nop_draw));
    patch_pointer(table, 21, reinterpret_cast<void*>(&nop_draw));
    patch_pointer(table, 20, reinterpret_cast<void*>(&nop_draw));
    patch_pointer(table, 38, reinterpret_cast<void*>(&nop_draw));
    patch_pointer(table, 39, reinterpret_cast<void*>(&nop_draw));
    patch_pointer(table, 40, reinterpret_cast<void*>(&nop_draw));
    patch_pointer(table, 41, reinterpret_cast<void*>(&nop_draw));
    patch_pointer(table, 42, reinterpret_cast<void*>(&nop_draw));
    patch_pointer(table, 49, reinterpret_cast<void*>(&nop_clear));
    patch_pointer(table, 52, reinterpret_cast<void*>(&nop_clear));
    patch_pointer(table, 50, reinterpret_cast<void*>(&nop_clear));
    patch_pointer(table, 51, reinterpret_cast<void*>(&nop_clear));
}

void patch_swap_chain(IDXGISwapChain* swap_chain) {
    if (!swap_chain) return;
    void** table = *reinterpret_cast<void***>(swap_chain);
    if (!table) return;
    patch_pointer(table, 8, reinterpret_cast<void*>(&nop_present));
}

using CreateDeviceFn = HRESULT(WINAPI*)(IDXGIAdapter*, D3D_DRIVER_TYPE, HMODULE, UINT,
                                        const D3D_FEATURE_LEVEL*, UINT, UINT,
                                        ID3D11Device**, D3D_FEATURE_LEVEL*,
                                        ID3D11DeviceContext**);
using CreateDeviceAndSwapChainFn = HRESULT(WINAPI*)(IDXGIAdapter*, D3D_DRIVER_TYPE, HMODULE, UINT,
                                                     const D3D_FEATURE_LEVEL*, UINT, UINT,
                                                     const DXGI_SWAP_CHAIN_DESC*, IDXGISwapChain**,
                                                     ID3D11Device**, D3D_FEATURE_LEVEL*,
                                                     ID3D11DeviceContext**);

}  // namespace

extern "C" __declspec(dllexport) HRESULT WINAPI D3D11CreateDevice(
    IDXGIAdapter* adapter, D3D_DRIVER_TYPE driver_type, HMODULE software,
    UINT flags, const D3D_FEATURE_LEVEL* feature_levels, UINT feature_level_count,
    UINT sdk_version, ID3D11Device** device, D3D_FEATURE_LEVEL* feature_level,
    ID3D11DeviceContext** context) {
    install_chat_limit();
    install_chat_bridge();
    HMODULE module = original_module();
    if (!module) {
        log_line("D3D11 proxy: original d3d11.dll not found");
        return E_FAIL;
    }
    auto function = reinterpret_cast<CreateDeviceFn>(GetProcAddress(module, "D3D11CreateDevice"));
    if (!function) {
        log_line("D3D11 proxy: D3D11CreateDevice export not found");
        return E_FAIL;
    }
    HRESULT result = function(adapter, driver_type, software, flags, feature_levels,
                              feature_level_count, sdk_version, device, feature_level, context);
    if (SUCCEEDED(result)) {
        patch_context(context ? *context : nullptr);
        log_line("D3D11 proxy: device created, draw calls disabled");
    }
    return result;
}

extern "C" __declspec(dllexport) HRESULT WINAPI D3D11CreateDeviceAndSwapChain(
    IDXGIAdapter* adapter, D3D_DRIVER_TYPE driver_type, HMODULE software,
    UINT flags, const D3D_FEATURE_LEVEL* feature_levels, UINT feature_level_count,
    UINT sdk_version, const DXGI_SWAP_CHAIN_DESC* swap_chain_desc,
    IDXGISwapChain** swap_chain, ID3D11Device** device,
    D3D_FEATURE_LEVEL* feature_level, ID3D11DeviceContext** context) {
    install_chat_limit();
    install_chat_bridge();
    HMODULE module = original_module();
    if (!module) {
        log_line("D3D11 proxy: original d3d11.dll not found");
        return E_FAIL;
    }
    auto function = reinterpret_cast<CreateDeviceAndSwapChainFn>(
        GetProcAddress(module, "D3D11CreateDeviceAndSwapChain"));
    if (!function) {
        log_line("D3D11 proxy: D3D11CreateDeviceAndSwapChain export not found");
        return E_FAIL;
    }
    HRESULT result = function(adapter, driver_type, software, flags, feature_levels,
                              feature_level_count, sdk_version, swap_chain_desc, swap_chain,
                              device, feature_level, context);
    if (SUCCEEDED(result)) {
        patch_context(context ? *context : nullptr);
        patch_swap_chain(swap_chain ? *swap_chain : nullptr);
        log_line("D3D11 proxy: swap-chain created, draw calls and Present disabled");
    }
    return result;
}
