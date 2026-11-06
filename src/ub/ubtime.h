// ns-3-ub time arithmetic, reproduced bit for bit (host side).
//
// ns-3-ub runs with Time resolution = picoseconds (ub-case-runner.cc SetResolution(PS)) and the
// 128-bit int64x64_t (int64x64-128.cc). A serialization time is
//     DataRate::CalculateBitsTxTime(bits) = Seconds(int64x64_t(bits) / m_bps)
// i.e. Q64.64 division (Udiv, below, verbatim), then Time::From(v, S): v *= int64x64_t(10^12)
// (Umul, truncating its lowest 64 bits), then Time(v) = v.Round() (half away from zero).
// Every per-packet time in the model is a table lookup filled by these functions.
#pragma once
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <string>
#include <stdexcept>

namespace ub {

typedef unsigned __int128 u128;

inline u128 i64x64_udiv(u128 a, u128 b)
{
    u128 rem = a, den = b;
    u128 quo = rem / den;
    rem = rem % den;
    u128 result = quo;
    const uint64_t DIGITS = 64;
    const u128 HI = (u128)1 << 127;
    uint64_t digis = 0, shift = 0;
    while ((shift < DIGITS) && !(den & 0x1)) {
        ++shift;
        den >>= 1;
    }
    while ((digis < DIGITS) && (rem != 0)) {
        while ((digis + shift < DIGITS) && !(rem & HI)) {
            ++shift;
            rem <<= 1;
        }
        while ((digis + shift < DIGITS) && (!(den & 0x1) || (rem < den))) {
            ++shift;
            den >>= 1;
        }
        quo = rem / den;
        rem = rem % den;
        result <<= shift;
        result += quo;
        digis += shift;
        shift = 0;
    }
    if (digis < DIGITS)
        result <<= (DIGITS - digis);
    return result;
}

inline u128 i64x64_umul(u128 a, u128 b)
{
    const u128 LO = ((u128)1 << 64) - 1;
    u128 al = a & LO, bl = b & LO, ah = a >> 64, bh = b >> 64;
    u128 res = (al * bl) >> 64;
    res += ah * bl + al * bh;
    res += (ah * bh) << 64;
    return res;
}

// Round(): add one half, take the integer part (values here are non-negative)
inline int64_t i64x64_round(u128 v) { return (int64_t)((v + ((u128)1 << 63)) >> 64); }

// Seconds(int64x64_t(bits) / bps) in ps
inline int64_t tx_ps_bits(uint64_t bps, uint32_t bits)
{
    if (bits == 0)
        return 0;
    u128 q = i64x64_udiv((u128)bits << 64, (u128)bps << 64);
    if (q == 0)
        return 0; // Time::From returns Time() for a zero value
    return i64x64_round(i64x64_umul(q, (u128)1000000000000ull << 64));
}
inline int64_t tx_ps(uint64_t bps, uint32_t bytes) { return tx_ps_bits(bps, bytes * 8u); }

// ns-3 attribute strings: "400Gbps", "20ns", "+5ns", "1.5us" (Time parses a double for the value)
inline int64_t parse_time_ps(std::string s)
{
    while (!s.empty() && (s[0] == '+' || s[0] == ' '))
        s.erase(0, 1);
    size_t i = 0;
    while (i < s.size() && (isdigit((unsigned char)s[i]) || s[i] == '.' || s[i] == 'e' || s[i] == '-'))
        ++i;
    std::string num = s.substr(0, i), unit = s.substr(i);
    if (num.empty())
        throw std::runtime_error("bad time " + s);
    bool integral = num.find_first_of(".e") == std::string::npos;
    int64_t mul;
    if (unit == "ps") mul = 1;
    else if (unit == "ns") mul = 1000;
    else if (unit == "us") mul = 1000000;
    else if (unit == "ms") mul = 1000000000ll;
    else if (unit == "s" || unit.empty()) mul = 1000000000000ll;
    else throw std::runtime_error("bad time unit " + s);
    if (integral)
        return strtoll(num.c_str(), nullptr, 10) * mul;
    // ns-3 converts the double through int64x64_t; values used in cases are exact in ps
    double v = strtod(num.c_str(), nullptr) * (double)mul;
    int64_t r = (int64_t)(v + 0.5);
    if ((double)r != v)
        throw std::runtime_error("time not a whole number of ps: " + s);
    return r;
}

inline uint64_t parse_rate_bps(std::string s)
{
    size_t i = 0;
    while (i < s.size() && (isdigit((unsigned char)s[i]) || s[i] == '.'))
        ++i;
    std::string num = s.substr(0, i), unit = s.substr(i);
    double v = strtod(num.c_str(), nullptr);
    double mul;
    if (unit == "bps" || unit == "b/s") mul = 1;
    else if (unit == "Kbps" || unit == "kbps") mul = 1e3;
    else if (unit == "Mbps") mul = 1e6;
    else if (unit == "Gbps") mul = 1e9;
    else if (unit == "Tbps") mul = 1e12;
    else throw std::runtime_error("bad rate " + s);
    uint64_t r = (uint64_t)(v * mul + 0.5);
    if ((double)r != v * mul)
        throw std::runtime_error("rate not whole bps: " + s);
    return r;
}

} // namespace ub
