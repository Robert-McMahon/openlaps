cmake_minimum_required(VERSION 3.13)

include(pico_sdk_import.cmake)

project(openlaps_gnss_receiver_interface C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

set(TIMING_TRANSPORT "USB" CACHE STRING "Host transport: USB or UART")
set_property(CACHE TIMING_TRANSPORT PROPERTY STRINGS USB UART)
string(TOUPPER "${TIMING_TRANSPORT}" TIMING_TRANSPORT)
if(NOT TIMING_TRANSPORT STREQUAL "USB" AND NOT TIMING_TRANSPORT STREQUAL "UART")
    message(FATAL_ERROR "TIMING_TRANSPORT must be USB or UART")
endif()

pico_sdk_init()

add_executable(openlaps-gnss-receiver-interface
    main.c
    association.c
    nmea.c
)
target_compile_options(openlaps-gnss-receiver-interface PRIVATE -Wall -Wextra -Werror)
target_link_libraries(openlaps-gnss-receiver-interface PRIVATE pico_stdlib hardware_gpio hardware_timer hardware_uart)

# The data relay carries the receiver's NMEA to the host and the host's RTCM
# and startup commands back, over the RP2040's internal USB CDC. It is a
# separate channel from the timing transport on purpose: TH1 lines and NMEA
# have different consumers on the host and cannot share one device node, so
# the relay requires TIMING_TRANSPORT=UART and main.c #errors otherwise.
option(GNSS_DATA_RELAY "Relay receiver NMEA/RTCM over the internal USB CDC" ON)

if(TIMING_TRANSPORT STREQUAL "USB")
    if(GNSS_DATA_RELAY)
        message(FATAL_ERROR "GNSS_DATA_RELAY requires TIMING_TRANSPORT=UART")
    endif()
    target_compile_definitions(openlaps-gnss-receiver-interface PRIVATE TIMING_TRANSPORT_USB=1)
    target_link_libraries(openlaps-gnss-receiver-interface PRIVATE pico_stdio_usb)
    pico_enable_stdio_usb(openlaps-gnss-receiver-interface 1)
    pico_enable_stdio_uart(openlaps-gnss-receiver-interface 0)
else()
    target_compile_definitions(openlaps-gnss-receiver-interface PRIVATE TIMING_TRANSPORT_UART=1)
    pico_enable_stdio_uart(openlaps-gnss-receiver-interface 0)
    if(GNSS_DATA_RELAY)
        target_compile_definitions(openlaps-gnss-receiver-interface PRIVATE GNSS_DATA_RELAY=1)
        target_link_libraries(openlaps-gnss-receiver-interface PRIVATE pico_stdio_usb)
        pico_enable_stdio_usb(openlaps-gnss-receiver-interface 1)
    else()
        pico_enable_stdio_usb(openlaps-gnss-receiver-interface 0)
    endif()
endif()

pico_add_extra_outputs(openlaps-gnss-receiver-interface)
