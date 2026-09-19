import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, wait
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).parent
PUBLIC = ROOT / 'public'
PORT = int(os.environ.get('PORT', '3000'))
SEARCH_TIMEOUT = float(os.environ.get('SEARCH_TIMEOUT', '5'))
CACHE_TTL = int(os.environ.get('SEARCH_CACHE_TTL', '120'))
VERSION = '0.4.0'
_SEARCH_CACHE = {}
_CACHE_LOCK = __import__('threading').Lock()
STOPWORDS = {'a','an','and','are','as','at','be','by','for','from','how','in','is','it','of','on','or','that','the','this','to','with','using','use','what','why','when','where','which','who','study','paper','papers','research','about'}
_SUFFIXES = ('ization','ations','ation','ments','ment','ingly','edly','ing','ed','es','s')
def clean(s=''): return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', str(s or ''))).strip()
def get_text(el): return clean(''.join(el.itertext())) if el is not None else ''
def normalize_token(token):
 t=token.lower().strip('.-')
 for suffix in _SUFFIXES:
  if len(t)>len(suffix)+3 and t.endswith(suffix): t=t[:-len(suffix)]; break
 return t
def make_terms(q):
 phrases=[re.sub(r'\s+',' ',p.strip().lower()) for p in re.findall(r'"([^"]+)"',q) if p.strip()]
 bare=re.sub(r'"[^"]+"',' ',q.lower()); raw=re.findall(r'[\w+#.-]+',bare,flags=re.UNICODE); terms=[]
 for t in raw:
  t=normalize_token(t)
  if t and t not in STOPWORDS and len(t)>=2 and t not in terms: terms.append(t)
 return terms,phrases
def arxiv_query(q):
 terms,phrases=make_terms(q); parts=[f'all:"{p}"' for p in phrases]+[f'all:{t}' for t in terms]; return ' AND '.join(parts) or f'all:{q.strip()}'
def pmc_query(q):
 terms,phrases=make_terms(q); parts=[f'"{p}"[Title/Abstract]' for p in phrases]+[f'{t}[Title/Abstract]' for t in terms]; return ' AND '.join(parts) or q.strip()
def doaj_query(q):
 terms,phrases=make_terms(q); return ' AND '.join([f'"{p}"' for p in phrases]+terms) or q.strip()
def token_set(text): return {normalize_token(x) for x in re.findall(r'[\w+#.-]+',text.lower(),flags=re.UNICODE) if x}
def text_for_match(r): return ' '.join([r.get('title',''),r.get('abstract',''),' '.join(r.get('tags',[])),' '.join(r.get('authors',[]))])
def rank_and_filter(results,q):
 terms,phrases=make_terms(q); qnorm=re.sub(r'\s+',' ',q.strip().lower()); scored=[]
 for r in results:
  all_text=text_for_match(r); hay=all_text.lower(); title=r.get('title','').lower(); tokens=token_set(all_text)
  if terms and any(t not in tokens for t in terms): continue
  title_tokens=token_set(title); phrase_hits=sum(1 for p in phrases if p in hay); title_hits=sum(1 for t in terms if t in title_tokens); exact_title=qnorm in title and bool(qnorm)
  score=(1000 if exact_title else 0)+(120*phrase_hits)+(35*title_hits)+(10*len(terms)); scored.append((score,r))
 scored.sort(key=lambda x:(x[0],x[1].get('date') or ''),reverse=True); return [r for _,r in scored]
def query_key(q,sources,limit): return (re.sub(r'\s+',' ',q.strip().lower()),tuple(sorted(sources)),int(limit),VERSION)
def cache_get(key):
 now=time.time()
 with _CACHE_LOCK:
  item=_SEARCH_CACHE.get(key)
  if item and item[0]>now: return item[1]
  if item: _SEARCH_CACHE.pop(key,None)
def cache_set(key,value):
 with _CACHE_LOCK:
  _SEARCH_CACHE[key]=(time.time()+CACHE_TTL,value)
  if len(_SEARCH_CACHE)>100: _SEARCH_CACHE.pop(min(_SEARCH_CACHE,key=lambda k:_SEARCH_CACHE[k][0]),None)
def url_json(url,headers=None,timeout=SEARCH_TIMEOUT):
 req=urllib.request.Request(url,headers=headers or {'User-Agent':f'PaperLens/{VERSION} (student research tool)'})
 with urllib.request.urlopen(req,timeout=timeout) as r: return json.loads(r.read().decode('utf-8'))
def url_text(url,headers=None,timeout=SEARCH_TIMEOUT):
 req=urllib.request.Request(url,headers=headers or {'User-Agent':f'PaperLens/{VERSION} (student research tool)'})
 with urllib.request.urlopen(req,timeout=timeout) as r: return r.read().decode('utf-8',errors='replace')
def search_arxiv(q,limit=10):
 params=urllib.parse.urlencode({'search_query':arxiv_query(q),'start':0,'max_results':limit,'sortBy':'relevance','sortOrder':'descending'}); root=ET.fromstring(url_text('https://export.arxiv.org/api/query?'+params,timeout=SEARCH_TIMEOUT)); ns={'a':'http://www.w3.org/2005/Atom'}; out=[]
 for e in root.findall('a:entry',ns):
  idurl=(e.findtext('a:id','',ns) or '').replace('http:','https:',1); arid=idurl.split('/abs/')[-1]; authors=[get_text(a.find('a:name',ns)) for a in e.findall('a:author',ns)]
  out.append({'id':'arxiv:'+arid,'source':'arxiv','title':get_text(e.find('a:title',ns)),'authors':[x for x in authors if x],'abstract':get_text(e.find('a:summary',ns)),'date':e.findtext('a:published','',ns) or e.findtext('a:updated','',ns),'url':idurl,'pdfUrl':f'https://arxiv.org/pdf/{arid}','tags':[c.attrib.get('term') for c in e.findall('a:category',ns)][:5],'openAccess':True})
 return out
def search_pmc(q,limit=10):
 base='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'; params=urllib.parse.urlencode({'db':'pmc','term':pmc_query(q),'retmode':'json','retmax':limit,'sort':'relevance','tool':'PaperLens','email':'local@example.com'}); ids=url_json(base+'esearch.fcgi?'+params,timeout=SEARCH_TIMEOUT).get('esearchresult',{}).get('idlist',[])
 if not ids:return []
 params=urllib.parse.urlencode({'db':'pmc','id':','.join(ids),'retmode':'xml','tool':'PaperLens','email':'local@example.com'}); root=ET.fromstring(url_text(base+'efetch.fcgi?'+params,timeout=SEARCH_TIMEOUT)); out=[]
 for art in root.findall('.//article'):
  title=get_text(art.find('.//article-title')); abstract=' '.join(get_text(x) for x in art.findall('.//abstract')); pmcid=art.findtext('.//article-id[@pub-id-type="pmcid"]') or ''; pmc_id=pmcid[3:] if pmcid.startswith('PMC') else (art.findtext('.//article-id[@pub-id-type="pmid"]') or '')
  authors=[]
  for c in art.findall('.//contrib[@contrib-type="author"]')[:8]:
   name=' '.join(x for x in (get_text(c.find('name/given-names')),get_text(c.find('name/surname'))) if x)
   if name: authors.append(name)
  date=art.findtext('.//pub-date/year') or ''
  if pmc_id: out.append({'id':'pmc:'+pmc_id,'source':'pmc','title':title or 'Untitled PMC record','authors':authors,'abstract':clean(abstract),'date':date,'url':f'https://pmc.ncbi.nlm.nih.gov/articles/PMC{pmc_id}/','pdfUrl':f'https://pmc.ncbi.nlm.nih.gov/articles/PMC{pmc_id}/pdf/','tags':['PMC'],'openAccess':True})
 return out
def search_doaj(q,limit=10):
 u='https://doaj.org/api/search/articles/'+urllib.parse.quote(doaj_query(q),safe='')+'?'+urllib.parse.urlencode({'page':1,'pageSize':limit}); js=url_json(u,timeout=SEARCH_TIMEOUT); out=[]
 for x in js.get('results',[]):
  b=x.get('bibjson',{}); doi=next((i.get('id') for i in b.get('identifier',[]) if str(i.get('type','')).lower()=='doi'),None); links=b.get('link') or []; link=(links[0].get('url') if links else None) or (f'https://doi.org/{doi}' if doi else f'https://doaj.org/article/{x.get("id")}'); full=next((l.get('url') for l in links if l.get('type')=='fulltext'),None) or link; authors=[a.get('name') or ' '.join([a.get('firstname',''),a.get('lastname','')]).strip() for a in b.get('author',[])]; out.append({'id':'doaj:'+str(x.get('id')),'source':'doaj','title':b.get('title') or 'Untitled DOAJ record','authors':[a for a in authors if a][:8],'abstract':clean(b.get('abstract','')),'date':str(b.get('year') or x.get('created_date','')),'url':link,'pdfUrl':full,'tags':[s if isinstance(s,str) else s.get('term','') for s in b.get('subject',[])][:5],'openAccess':True})
 return out
def search_gutenberg(q,limit=6):
 js=url_json('https://gutendex.com/books?'+urllib.parse.urlencode({'search':q,'page':1}),timeout=min(SEARCH_TIMEOUT,3.5)); out=[]
 for x in js.get('results',[])[:limit]:
  fmt=x.get('formats',{}); out.append({'id':'gutenberg:'+str(x.get('id')),'source':'gutenberg','title':x.get('title') or 'Untitled book','authors':[a.get('name','') for a in x.get('authors',[]) if a.get('name')][:8],'abstract':f'Public-domain ebook. {x.get("download_count",0)} downloads.','date':None,'url':f'https://www.gutenberg.org/ebooks/{x.get("id")}','pdfUrl':fmt.get('application/pdf') or fmt.get('text/html') or f'https://www.gutenberg.org/ebooks/{x.get("id")}','tags':['Book'],'openAccess':True})
 return out
def do_search(q,sources,limit=10):
 funcs={'arxiv':search_arxiv,'pmc':search_pmc,'doaj':search_doaj,'gutenberg':search_gutenberg}; sources=[s for s in sources if s in funcs]; key=query_key(q,sources,limit); cached=cache_get(key)
 if cached is not None: cached=dict(cached); cached['cached']=True; return cached
 executor=ThreadPoolExecutor(max_workers=len(sources)); fm={executor.submit(funcs[s],q,limit if s!='gutenberg' else min(6,limit)):s for s in sources}; done,not_done=wait(list(fm),timeout=SEARCH_TIMEOUT); raw=[]; status={}
 for fut in done:
  s=fm[fut]
  try: xs=fut.result(); raw.extend(xs); status[s]={'source':s,'count':len(xs),'error':None}
  except Exception as e: status[s]={'source':s,'count':0,'error':f'{type(e).__name__}: {e}'}
 for fut in not_done:
  s=fm[fut]; status[s]={'source':s,'count':0,'error':f'timeout after {SEARCH_TIMEOUT:.1f}s'}; fut.cancel()
 executor.shutdown(wait=False,cancel_futures=True); result={'query':q,'results':rank_and_filter(raw,q)[:60],'sourceStatus':[status[s] for s in sources],'cached':False,'version':VERSION}; cache_set(key,result); return result
class Handler(SimpleHTTPRequestHandler):
 def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(PUBLIC),**kwargs)
 def send_json(self,code,data):
  raw=json.dumps(data).encode(); self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Access-Control-Allow-Origin','*'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
 def do_GET(self):
  parsed=urllib.parse.urlparse(self.path)
  if parsed.path=='/api/search':
   qs=urllib.parse.parse_qs(parsed.query); q=qs.get('q',[''])[0].strip()
   if not q:return self.send_json(400,{'error':'Missing q'})
   sources=[s.strip() for s in qs.get('sources',['arxiv,pmc,doaj,gutenberg'])[0].split(',') if s.strip()]
   try: limit=min(12,max(4,int(qs.get('limit',['10'])[0])))
   except ValueError: limit=10
   try:return self.send_json(200,do_search(q,sources,limit))
   except Exception as e:return self.send_json(500,{'error':str(e),'version':VERSION})
  if parsed.path=='/api/health': return self.send_json(200,{'ok':True,'version':VERSION,'claude':bool(os.environ.get('ANTHROPIC_API_KEY')),'searchTimeout':SEARCH_TIMEOUT,'cacheTtl':CACHE_TTL})
  return super().do_GET()
 def do_POST(self):
  parsed=urllib.parse.urlparse(self.path)
  if parsed.path!='/api/summarize':return self.send_json(404,{'error':'Not found'})
  n=int(self.headers.get('Content-Length','0')); body=json.loads(self.rfile.read(n) or '{}'); title=body.get('title',''); input_text=(body.get('fullText') or body.get('abstract') or '').strip()
  if not input_text:return self.send_json(400,{'error':'No paper text supplied'})
  key=os.environ.get('ANTHROPIC_API_KEY')
  if not key:
   pieces=re.split(r'(?<=[.!?])\s+',clean(input_text))[:3]; return self.send_json(200,{'summary':(title+': ' if title else '')+' '.join(pieces),'mode':'local-fallback'})
  model=os.environ.get('ANTHROPIC_MODEL','claude-sonnet-4-6'); prompt='You are a research-reading assistant for undergraduate students. Summarize the paper in 2-3 sentences, plain English, without overselling the result. State the question/method and the main finding or implication. Do not invent details.\n\nTitle: '+(title or 'Unknown')+'\n\nPaper text:\n'+input_text[:50000]; payload=json.dumps({'model':model,'max_tokens':260,'messages':[{'role':'user','content':prompt}]}).encode(); req=urllib.request.Request('https://api.anthropic.com/v1/messages',data=payload,headers={'Content-Type':'application/json','x-api-key':key,'anthropic-version':'2023-06-01'},method='POST')
  try:
   with urllib.request.urlopen(req,timeout=45) as r:data=json.loads(r.read().decode())
   return self.send_json(200,{'summary':' '.join(x.get('text','') for x in data.get('content',[])).strip(),'mode':'claude','model':model})
  except Exception as e:return self.send_json(500,{'error':str(e)})
if __name__=='__main__': ThreadingHTTPServer(('0.0.0.0',PORT),Handler).serve_forever()
