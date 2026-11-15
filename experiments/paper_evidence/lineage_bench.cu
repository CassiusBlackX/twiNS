// Fixed-input GPU microbenchmark using the production cpush/fp_step/ccmp_walk/ccmp.
// Reconstructed contiguous tail layout, repeated access order: no extrapolation to production
// pointer locality or decomposition of overlapped end-to-end wall time is justified.
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <vector>
#include "rdma/rstep.cuh"
using namespace rdma;
#ifndef TWINS_USE_HIP
#define hipEventSynchronize cudaEventSynchronize
#define hipEventElapsedTime cudaEventElapsedTime
#define hipEventDestroy cudaEventDestroy
#endif
#define OK(x) do{auto e=(x);if(e!=hipSuccess){fprintf(stderr,"%s: %s\n",#x,hipGetErrorString(e));exit(1);}}while(0)
struct Input {Chain a,b;int expected;int inexact;};
struct Result {uint64_t h1,h2;int value,bad;};
__global__ void compare_inputs(Sim s,const Input *inputs,Result *results,int n,int fp){
 int i=blockIdx.x*blockDim.x+threadIdx.x;if(i>=n)return;
 bool bad=false;int r=fp?ccmp(s,0,inputs[i].a,0,inputs[i].b,&bad):ccmp_walk(s.root_rank,0,inputs[i].a,0,inputs[i].b,&bad);
 results[i]={0,0,r,(int)bad};
}
__global__ void update_inputs(const twins_corpus::Sample *samples,Result *results,int n,bool construct){
 int i=blockIdx.x*blockDim.x+threadIdx.x;if(i>=n)return;
 const auto &sample=samples[i];Chain c{};c.root=1;uint64_t h1=0,h2=0;
 for(int k=(int)sample.na-1;k>=0;k--)
  for(uint32_t j=0;j<sample.a[k].cnt;j++) {
   if(construct){Chain out;cpush(&out,sample.a[k].code,c);c=out;}
   else fp_step(h1,h2,sample.a[k].code);
  }
 results[i]={construct?c.hash:h1,construct?c.hash2:h2,0,0};
}
__global__ void saturation_fixture(const uint32_t *ranks,int *result) {
 Chain a{},b{};a.root=1;b.root=2;a.r[0]={ccode(1000,0),0xFFFFFFFFu};b.r[0]=a.r[0];
 a.r[1]={ccode(900,0),1};b.r[1]={ccode(800,0),1};
 bool bad=false;result[0]=ccmp_walk(ranks,0,a,0,b,&bad)==0;
 RunCursor c;c.init(a,a.r[0].code);result[1]=c.left==0xFFFFFFFFu;
 Run pa[PRE]{},pb[PRE]{};cprefix(pa,a.r[0].code,a);cprefix(pb,0,b);
 result[2]=pa[0].cnt==0xFFFFFFFFu;result[3]=pcmp(pa,pb)==0;
 a.r[0].cnt=b.r[0].cnt=8;cprefix(pa,0,a);cprefix(pb,0,b);
 result[4]=pcmp(pa,pb)==-1;result[5]=!lead_lossy(a.r[0].code>>CODE_ORD_BITS);
}
static void check_saturation(){
 uint32_t *ranks;int *r;uint32_t rank[2]={0,1};int result[6]{};
 OK(hipMalloc((void**)&ranks,sizeof(rank)));OK(hipMemcpy(ranks,rank,sizeof(rank),hipMemcpyHostToDevice));
 OK(hipMalloc((void**)&r,sizeof(result)));saturation_fixture<<<1,1>>>(ranks,r);
 OK(hipDeviceSynchronize());OK(hipMemcpy(result,r,sizeof(result),hipMemcpyDeviceToHost));
 for(int i=0;i<6;i++)if(!result[i]){fprintf(stderr,"saturation fixture check %d failed\n",i);exit(5);}
 OK(hipFree(r));OK(hipFree(ranks));
 printf("{\"fixture\":\"saturated_run_and_prefix\",\"passed\":true,\"checks\":6,\"finite_positive_control\":true}\n");
}
template<typename T>T *upload(const std::vector<T>&v){T*p=nullptr;OK(hipMalloc((void**)&p,std::max<size_t>(1,v.size())*sizeof(T)));if(v.size())OK(hipMemcpy(p,v.data(),v.size()*sizeof(T),hipMemcpyHostToDevice));return p;}
int main(int argc,char**argv){
 if(argc==2 && std::string(argv[1])=="--boundary-only"){check_saturation();return 0;}
 if(argc!=2){fprintf(stderr,"usage: lineage-bench output.corpus\n");return 1;}
 std::ifstream file(argv[1],std::ios::binary);std::vector<twins_corpus::Sample> samples;
 for(int i=0;i<twins_corpus::CAP;i++){
  twins_corpus::Sample x;file.read((char*)&x,sizeof(x));if(!file){fprintf(stderr,"incomplete corpus\n");return 2;}
  if(x.flags==1 && !x.inexact && x.na && x.nb)samples.push_back(x);
 }
 if(samples.empty()){fprintf(stderr,"no complete, exact samples\n");return 3;}
 std::vector<Input> inputs;std::vector<TailNode> tails;std::vector<uint32_t> ranks;uint64_t logical_events=0;
 for(size_t i=0;i<samples.size();i++){
  auto &x=samples[i];Input in{};in.expected=x.result;in.inexact=x.inexact;
  uint32_t ar=x.roota?2*i+1:0,br=x.rootb?(x.rootb==x.roota?ar:2*i+2):0;
  ranks.push_back(x.ranka);ranks.push_back(x.rankb);
  auto make=[&](const twins_corpus::Run *runs,uint32_t count,uint32_t root,uint64_t h1,uint64_t h2){
   Chain c{};c.root=root;c.hash=h1;c.hash2=h2;uint64_t length=0;
   for(uint32_t k=0;k<count;k++){length+=runs[k].cnt;if(k<RUNS)c.r[k]={runs[k].code,runs[k].cnt};}
   c.pad=(uint32_t)std::min<uint64_t>(length,0xffffffffu);
   if(count>RUNS){c.tail=tails.size()+1;for(uint32_t k=RUNS;k<count;k++){TailNode t{};t.code=runs[k].code;t.cnt=runs[k].cnt;t.pos=tails.size()+1;t.next=k+1<count?t.pos+1:0;tails.push_back(t);}}
   return c;
  };
  in.a=make(x.a,x.na,ar,x.a1,x.a2);in.b=make(x.b,x.nb,br,x.b1,x.b2);inputs.push_back(in);
  for(uint32_t k=0;k<x.na;k++)logical_events+=x.a[k].cnt;
 }
 // Walk inputs include the root-start prefix; FP intentionally covers only its suffix.
 // Recover that suffix length from both saved polynomial values; reject ambiguous/missing matches.
 for(auto &x:samples){
   uint64_t p1=1,p2=1,h1=0,h2=0;uint64_t matched=0,seen=0;int matches=0;
   if((x.a1&FP_P)==0 && x.a2==0){matches=1;matched=0;}
   for(uint32_t k=0;k<x.na;k++)for(uint32_t j=0;j<x.a[k].cnt;j++){
     uint64_t L=x.a[k].code>>CODE_ORD_BITS;
     h1=(h1+(uint64_t)((unsigned __int128)L*p1%FP_P))%FP_P;
     h2=(h2+(uint64_t)((unsigned __int128)L*p2%FP_P))%FP_P;
     p1=(uint64_t)((unsigned __int128)p1*1000003%FP_P);p2=(uint64_t)((unsigned __int128)p2*1000033%FP_P);++seen;
     if(h1==(x.a1&FP_P) && h2==x.a2){++matches;matched=seen;}
   }
   if(matches!=1){fprintf(stderr,"FP suffix matches %d; input rejected\n",matches);return 4;}
   uint64_t left=matched;uint32_t keep=0;
   for(uint32_t k=0;k<x.na && left;k++){auto r=x.a[k];if(r.cnt>left)r.cnt=left;x.a[keep++]=r;left-=r.cnt;}
   x.na=keep;
 }
 size_t cap=1;while(cap<tails.size())cap*=2;tails.resize(cap);
 TailPool original{};original.n=upload(tails);original.mask=cap-1;
 auto *dinput=upload(inputs);auto *dsamples=upload(samples);std::vector<Result> result(samples.size());auto *dresult=upload(result);auto *dranks=upload(ranks);
 uint64_t bases[2]={1000003,1000033};OK(hipMemcpyToSymbol(HIP_SYMBOL(c_fp),bases,sizeof(bases)));
 Memo memo{};const size_t mcap=1<<22;OK(hipMalloc((void**)&memo.we,mcap*sizeof(WalkEnt)));memo.walk_mask=mcap-1;OK(hipMemset(memo.we,0,mcap*sizeof(WalkEnt)));OK(hipMemcpyToSymbol(HIP_SYMBOL(g_memo),&memo,sizeof(memo)));
 Sim s{};s.root_rank=dranks;OK(hipMalloc((void**)&s.ev,40*8));OK(hipMemset(s.ev,0,40*8));
 TailPool construct{};construct.mask=(1<<22)-1;OK(hipMalloc((void**)&construct.n,(construct.mask+1)*sizeof(TailNode)));OK(hipMalloc((void**)&construct.cnt,8));
 check_saturation();
 printf("{\"samples\":%zu,\"logical_a_events\":%llu,\"tail_slots\":%zu,\"chain_bytes\":%zu,\"layout\":\"reconstructed contiguous tails; fixed sample order\"}\n",samples.size(),(unsigned long long)logical_events,cap,sizeof(Chain));
 for(int phase=0;phase<4;phase++){
  const char *name=phase==0?"fp_update":phase==1?"suffix_construction_including_fp":phase==2?"exact_walk_memo":"fingerprint_shortcut";
  for(int round=0;round<3;round++){
   OK(hipMemset(memo.we,0,mcap*sizeof(WalkEnt)));
   if(phase<2){OK(hipMemset(construct.cnt,0,8));OK(hipMemcpyToSymbol(HIP_SYMBOL(g_tail),&construct,sizeof(construct)));}
   else OK(hipMemcpyToSymbol(HIP_SYMBOL(g_tail),&original,sizeof(original)));
   // Warm the module and fixed layout outside the timed interval; reset only the memo.
   if(phase<2)update_inputs<<<1,64>>>(dsamples,dresult,(int)samples.size(),phase==1);
   else compare_inputs<<<1,64>>>(s,dinput,dresult,(int)samples.size(),phase==3);
   OK(hipDeviceSynchronize());OK(hipMemset(memo.we,0,mcap*sizeof(WalkEnt)));
   if(phase<2)OK(hipMemset(construct.cnt,0,8));
   hipEvent_t start,stop;OK(hipEventCreateWithFlags(&start,0));OK(hipEventCreateWithFlags(&stop,0));
   OK(hipEventRecord(start,0));
   if(phase<2)update_inputs<<<1,64>>>(dsamples,dresult,(int)samples.size(),phase==1);
   else compare_inputs<<<1,64>>>(s,dinput,dresult,(int)samples.size(),phase==3);
   OK(hipEventRecord(stop,0));OK(hipEventSynchronize(stop));float ms=0;OK(hipEventElapsedTime(&ms,start,stop));
   OK(hipMemcpy(result.data(),dresult,result.size()*sizeof(Result),hipMemcpyDeviceToHost));
   int mismatch=0;for(size_t i=0;i<result.size();i++){
    if(phase<2)mismatch+=(result[i].h1!=samples[i].a1 || result[i].h2!=samples[i].a2);
    else mismatch+=(result[i].value!=inputs[i].expected || result[i].bad!=inputs[i].inexact);
   }
   printf("{\"phase\":\"%s\",\"round\":%d,\"gpu_ms\":%.9f,\"mismatches\":%d,\"memo_initially_empty\":true}\n",name,round+1,ms,mismatch);
   // Cache-warmed second walk uses precisely the same inputs, data layout and table.
   if(phase==2){OK(hipEventRecord(start,0));compare_inputs<<<1,64>>>(s,dinput,dresult,(int)samples.size(),0);OK(hipEventRecord(stop,0));OK(hipEventSynchronize(stop));OK(hipEventElapsedTime(&ms,start,stop));OK(hipMemcpy(result.data(),dresult,result.size()*sizeof(Result),hipMemcpyDeviceToHost));int wrong=0;for(size_t i=0;i<result.size();i++)wrong+=(result[i].value!=inputs[i].expected || result[i].bad!=inputs[i].inexact);printf("{\"phase\":\"exact_walk_memo_warm\",\"round\":%d,\"gpu_ms\":%.9f,\"mismatches\":%d}\n",round+1,ms,wrong);}
   OK(hipEventDestroy(start));OK(hipEventDestroy(stop));
  }
 }
 return 0;
}
