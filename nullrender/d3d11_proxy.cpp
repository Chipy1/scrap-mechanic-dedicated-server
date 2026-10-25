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
