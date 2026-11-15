#ifndef TWINS_EVIDENCE_CORPUS_H
#define TWINS_EVIDENCE_CORPUS_H
#include <cstdint>
namespace twins_corpus {
constexpr int CAP=64, MAX_RUNS=65536;
struct Run {uint32_t code,cnt;};
struct Sample {
 uint32_t na,nb,flags,roota,rootb;
 int32_t ranka,rankb,result,inexact;
 uint64_t a1,a2,b1,b2;
 Run a[MAX_RUNS],b[MAX_RUNS];
};
}
#endif
