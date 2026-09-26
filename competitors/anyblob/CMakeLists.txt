cmake_minimum_required(VERSION 3.16)
project(anyblob-s3 CXX)

# AnyBlob (Durner, Leis, Neumann, VLDB 2023) as the paper ships it, built as
# a static library from the checkout next to this file. Its own CMake wants
# lld and links jemalloc, OpenSSL and liburing; the justfile points every one
# of those at a static archive so the result runs on a stock AL2023 AMI.
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
add_subdirectory(AnyBlob EXCLUDE_FROM_ALL)

add_executable(anyblob-s3 src/main.cpp)
target_link_libraries(anyblob-s3 PRIVATE AnyBlob)
target_compile_options(anyblob-s3 PRIVATE -Wall -Wextra)
