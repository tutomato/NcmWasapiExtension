/* RBJ biquads, independently computed for each actual stream rate.
 * Formula reference: https://www.w3.org/TR/audio-eq-cookbook/
 * Double precision filter state; float32 PCM in/out. No allocation in C loop.
 */
function validateEq(value) {
  if (!value || typeof value.enabled !== 'boolean' || !Array.isArray(value.bands) || value.bands.length > 10)
    throw Error('Invalid EQ configuration');
  const number = (n, lo, hi) => {
    if (typeof n !== 'number' || !Number.isFinite(n) || n < lo || n > hi) throw Error('EQ parameter out of range');
    return n;
  };
  return { enabled: value.enabled, preamp: number(value.preamp, -30, 0), bands: value.bands.map(b => {
    if (!b || typeof b.enabled !== 'boolean' || !['bell', 'low_shelf', 'high_shelf'].includes(b.type))
      throw Error('Invalid EQ band');
    return {enabled:b.enabled, type:b.type, frequency:number(b.frequency,20,20000),
      gain:number(b.gain,-18,18), q:number(b.q,0.2,20)};
  }) };
}
function eqCoefficients(b, rate) {
  if (!b.enabled || b.gain === 0 || b.frequency >= rate / 2) return [1,0,0,0,0];
  const w = 2*Math.PI*b.frequency/rate, c=Math.cos(w), A=Math.pow(10,b.gain/40);
  const alpha=Math.sin(w)/(2*b.q), t=2*Math.sqrt(A)*alpha;
  let b0,b1,b2,a0,a1,a2;
  if (b.type==='bell') {
    b0=1+alpha*A; b1=-2*c; b2=1-alpha*A;
    a0=1+alpha/A; a1=-2*c; a2=1-alpha/A;
  } else if (b.type==='low_shelf') {
    b0=A*((A+1)-(A-1)*c+t); b1=2*A*((A-1)-(A+1)*c); b2=A*((A+1)-(A-1)*c-t);
    a0=(A+1)+(A-1)*c+t; a1=-2*((A-1)+(A+1)*c); a2=(A+1)+(A-1)*c-t;
  } else {
    b0=A*((A+1)+(A-1)*c+t); b1=-2*A*((A-1)+(A+1)*c); b2=A*((A+1)+(A-1)*c-t);
    a0=(A+1)-(A-1)*c+t; a1=2*((A-1)-(A+1)*c); a2=(A+1)-(A-1)*c-t;
  }
  return [b0/a0,b1/a0,b2/a0,a1/a0,a2/a0];
}
function createParametricEq() {
  const module = new CModule(`
    typedef struct { double c[50], z[40], gain; } Bank;
    typedef struct { Bank bank[2]; unsigned int current, remaining, total; } State;
    unsigned int state_size(void) { return sizeof(State); }
    static double run(Bank *b, double x, unsigned int channel) {
      unsigned int k; x *= b->gain;
      for(k=0;k<10;k++) {
        double *c=b->c+k*5, *z=b->z+(k*2+channel)*2;
        double y=c[0]*x+z[0];
        z[0]=c[1]*x-c[3]*y+z[1]; z[1]=c[2]*x-c[4]*y;
        if(z[0]<1e-24 && z[0]>-1e-24) z[0]=0;
        if(z[1]<1e-24 && z[1]>-1e-24) z[1]=0;
        x=y;
      }
      return x;
    }
    void configure(State *s, const double *c, double gain, unsigned int ramp) {
      unsigned int i, next=1-s->current; Bank *b=&s->bank[next];
      for(i=0;i<50;i++) b->c[i]=c[i];
      for(i=0;i<40;i++) b->z[i]=0;
      b->gain=gain; s->remaining=ramp; s->total=ramp; s->current=next;
    }
    void reset(State *s) {
      unsigned int b,i; for(b=0;b<2;b++) {
        for(i=0;i<50;i++) s->bank[b].c[i]=(i%5==0)?1:0;
        for(i=0;i<40;i++) s->bank[b].z[i]=0;
        s->bank[b].gain=1;
      } s->current=0; s->remaining=0; s->total=0;
    }
    void fade_in(float *pcm, unsigned int frames, unsigned int channels,
        unsigned int position, unsigned int total) {
      unsigned int f,ch;
      for(f=0;f<frames && position+f<total;f++) {
        double x=(double)(position+f)/(total-1), gain=x*x*(3-2*x);
        for(ch=0;ch<channels;ch++) pcm[f*channels+ch]=(float)(pcm[f*channels+ch]*gain);
      }
    }
    void process(State *s, float *pcm, unsigned int frames, unsigned int channels) {
      unsigned int f,ch;
      for(f=0;f<frames;f++) {
        double mix=s->remaining?1.0-(double)s->remaining/s->total:1;
        for(ch=0;ch<channels;ch++) {
          double x=pcm[f*channels+ch];
          if(!(x<=3.402823466e38 && x>=-3.402823466e38)) x=0;
          double y=run(&s->bank[s->current],x,ch);
          if(s->remaining) y=y*mix+run(&s->bank[1-s->current],x,ch)*(1-mix);
          if(!(y<=3.402823466e38 && y>=-3.402823466e38)) y=0;
          pcm[f*channels+ch]=(float)y;
        }
        if(s->remaining) s->remaining--;
      }
    }
  `);
  const size=new NativeFunction(module.state_size,'uint',[])();
  if(size!==1472) throw Error('Unexpected EQ state size '+size);
  const configure=new NativeFunction(module.configure,'void',['pointer','pointer','double','uint'],{scheduling:'exclusive'});
  const reset=new NativeFunction(module.reset,'void',['pointer'],{scheduling:'exclusive'});
  const process=new NativeFunction(module.process,'void',['pointer','pointer','uint','uint'],{scheduling:'exclusive'});
  const fadeIn=new NativeFunction(module.fade_in,'void',['pointer','uint','uint','uint','uint'],{scheduling:'exclusive'});
  let config=validateEq({enabled:false,preamp:0,bands:[]}), generation=0;
  return {
    nativeModule: module, // Keep executable CModule memory alive with the DSP owner.
    set(value) { config=validateEq(value); generation++; return config; },
    get() { return config; },
    reset(stream) { if(stream.eqState) reset(stream.eqState); stream.eqGeneration=-1; },
    fadeIn(stream, pcm, frames) {
      const total=Math.max(2,Math.round(stream.rate*.005)),position=stream.startupFrames||0;
      if(position>=total)return;
      fadeIn(pcm,frames,stream.channels,position,total);
      stream.startupFrames=Math.min(total,position+frames);
    },
    process(stream, pcm, frames) {
      if(!config.enabled && !stream.eqState) return;
      if(!stream.eqState) { stream.eqState=Memory.alloc(size); reset(stream.eqState); stream.eqGeneration=-1; }
      if(stream.eqGeneration!==generation) {
        const c=Memory.alloc(50*8);
        for(let i=0;i<10;i++) {
          const b=config.enabled && config.bands[i];
          const values=b?eqCoefficients(b,stream.rate):[1,0,0,0,0];
          values.forEach((v,j)=>c.add((i*5+j)*8).writeDouble(v));
        }
        // A new/reset stream has no previous filtered signal to crossfade.
        // Apply its intended gain immediately; live edits retain the 20 ms blend.
        configure(stream.eqState,c,config.enabled?Math.pow(10,config.preamp/20):1,
          stream.eqGeneration===-1?0:Math.max(1,Math.round(stream.rate*.02)));
        stream.eqGeneration=generation;
      }
      process(stream.eqState,pcm,frames,stream.channels);
      if(config.enabled) stream.eqProcessedFrames=(stream.eqProcessedFrames||0)+frames;
    }
  };
}
