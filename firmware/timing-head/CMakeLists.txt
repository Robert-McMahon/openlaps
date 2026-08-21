cmake_minimum_required(VERSION 3.13)

include(pico_sdk_import.cmake)

project(openlaps_timing_head C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

set(TIMING_TRANSPORT "USB" CACHE STRING "Host transport: USB or UART")
set_property(CACHE TIMING_TRANSPORT PROPERTY STRINGS USB UART)
string(TOUPPER "${TIMING_TRANSPORT}" TIMING_TRANSPORT)
if(NOT TIMING_TRANSPORT STREQUAL "USB" AND NOT TIMING_TRANSPORT STREQUAL "UART")
    message(FATAL_ERROR "TIMING_TRANSPORT must be USB or UART")
endif()

pico_sdk_init()

add_executable(openlaps-timing-head
    main.c
    association.c
    nmea.c
)
target_compile_options(openlaps-timing-head PRIVATE -Wall -Wextra -Werror)
target_link_libraries(openlaps-timing-head PRIVATE pico_stdlib hardware_gpio hardware_timer hardware_uart)

if(TIMING_TRANSPORT STREQUAL "USB")
    target_compile_definitions(openlaps-timing-head PRIVATE TIMING_TRANSPORT_USB=1)
    target_link_libraries(openlaps-timing-head PRIVATE pico_stdio_usb)
    pico_enable_stdio_usb(openlaps-timing-head 1)
    pico_enable_stdio_uart(openlaps-timing-head 0)
else()
    target_compile_definitions(openlaps-timing-head PRIVATE TIMING_TRANSPORT_UART=1)
    pico_enable_stdio_usb(openlaps-timing-head 0)
    pico_enable_stdio_uart(openlaps-timing-head 0)
endif()

pico_add_extra_outputs(openlaps-timing-head)
