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
