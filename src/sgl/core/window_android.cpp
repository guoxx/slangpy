// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

#ifdef __ANDROID__

#include "sgl/core/window.h"
#include "sgl/core/platform.h"
#include "sgl/core/error.h"

#include <android/native_window.h>
#include <android/log.h>

#define LOG_TAG "SlangPy-Window"
#define LOGD(...) __android_log_print(ANDROID_LOG_DEBUG, LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wunused-parameter"

namespace sgl {

Window::Window(WindowDesc desc)
    : m_width(desc.width)
    , m_height(desc.height)
    , m_title(std::move(desc.title))
    , m_window(nullptr)
{
    // Android window initialization is minimal
    // The actual native window will be set via set_android_native_window()
}

Window::~Window()
{
    if (m_native_window) {
        ANativeWindow_release(m_native_window);
        m_native_window = nullptr;
    }
}

WindowHandle Window::window_handle() const
{
    WindowHandle handle{};
    handle.native_window = m_native_window;
    return handle;
}

void Window::set_android_native_window(uintptr_t native_window_ptr)
{
    LOGD("=== set_android_native_window called ===");
    LOGD("Input parameter (uintptr_t): %llu (0x%llx)",
         (unsigned long long)native_window_ptr, (unsigned long long)native_window_ptr);

    // Convert pointer value to ANativeWindow*
    m_native_window = reinterpret_cast<::ANativeWindow*>(native_window_ptr);
    LOGD("Converted to ANativeWindow*: %p", m_native_window);

    if (!m_native_window) {
        LOGE("Invalid ANativeWindow pointer (null after conversion)!");
        SGL_THROW("Invalid ANativeWindow pointer");
    }

    // Acquire reference to the native window
    LOGD("Calling ANativeWindow_acquire...");
    ANativeWindow_acquire(m_native_window);
    LOGD("ANativeWindow_acquire succeeded");

    // Query actual window dimensions
    LOGD("Querying window dimensions...");
    int32_t width = ANativeWindow_getWidth(m_native_window);
    int32_t height = ANativeWindow_getHeight(m_native_window);
    LOGD("ANativeWindow dimensions: %dx%d", width, height);

    if (width > 0 && height > 0) {
        m_width = static_cast<uint32_t>(width);
        m_height = static_cast<uint32_t>(height);
        LOGD("Window size updated to: %ux%u", m_width, m_height);
    } else {
        LOGE("Invalid window dimensions: %dx%d", width, height);
    }

    LOGD("=== set_android_native_window completed successfully ===");
}

void Window::set_width(uint32_t width)
{
    resize(width, m_height);
}

void Window::set_height(uint32_t height)
{
    resize(m_width, height);
}

void Window::set_size(uint2 size)
{
    resize(size.x, size.y);
}

void Window::resize(uint32_t width, uint32_t height)
{
    // No-op on Android - window size is managed by the system
    m_width = width;
    m_height = height;
}

int2 Window::position() const
{
    // Android windows don't have a position concept - always return (0, 0)
    return int2{0, 0};
}

void Window::set_position(int2 position)
{
    // No-op on Android - window position is managed by the system
    SGL_UNUSED(position);
}

void Window::set_title(std::string title)
{
    // No-op on Android - no window title concept
    m_title = std::move(title);
}

void Window::set_icon(const std::filesystem::path& path)
{
    // No-op on Android
}

void Window::close()
{
    m_should_close = true;
}

bool Window::should_close() const
{
    return m_should_close;
}

void Window::process_events()
{
    // No-op on Android - events are handled by Android system
}

void Window::set_clipboard(const std::string& text)
{
    // No-op on Android
}

std::optional<std::string> Window::get_clipboard() const
{
    // Not supported on Android
    return std::nullopt;
}

void Window::set_cursor_mode(CursorMode mode)
{
    // No-op on Android - no cursor concept
    m_cursor_mode = mode;
}

void Window::poll_gamepad_input()
{
    // No-op on Android
}

void Window::handle_window_size(uint32_t width, uint32_t height)
{
    m_width = width;
    m_height = height;
    if (m_on_resize)
        m_on_resize(width, height);
}

void Window::handle_keyboard_event(const KeyboardEvent& event)
{
    if (m_on_keyboard_event)
        m_on_keyboard_event(event);
}

void Window::handle_mouse_event(const MouseEvent& event)
{
    if (m_on_mouse_event)
        m_on_mouse_event(event);
}

void Window::handle_gamepad_event(const GamepadEvent& event)
{
    if (m_on_gamepad_event)
        m_on_gamepad_event(event);
}

void Window::handle_drop_files(std::span<const char*> files)
{
    if (m_on_drop_files)
        m_on_drop_files(files);
}

std::string Window::to_string() const
{
    return fmt::format(
        "Window(\n"
        "  width = {},\n"
        "  height = {},\n"
        "  title = \"{}\"\n"
        ")",
        m_width,
        m_height,
        m_title
    );
}

} // namespace sgl

#pragma clang diagnostic pop

#endif // __ANDROID__
