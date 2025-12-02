import re
import time
import asyncio
import nest_asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, HTTPException
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# Permite loops aninhados
nest_asyncio.apply()

# URL DO TRF6
URL = "https://pje1g.trf6.jus.br/consultapublica/ConsultaPublica/listView.seam"

# Regex Padrão CNJ
CNJ_RE = re.compile(r"\b\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}\b")

# Filtro para NÃO retornar ruídos
UNWANTED_RE = re.compile(
    r"(documentos?\s+juntados|documento\b|certid[aã]o|visualizar|"
    r"pjeoffice|indispon[ií]vel|aplicativo\s+pjeoffice|"
    r"página\b|resultados?\s+encontrados|recibo)",
    re.IGNORECASE,
)

def _norm(txt: str) -> str:
    return re.sub(r"\s+", " ", (txt or "")).strip()

def sanitize_doc(doc: str) -> str:
    return re.sub(r"\D+", "", doc or "")

# ===== Concurrency + Cache =====
SEMA = asyncio.Semaphore(1)
CACHE_TTL = 300
_cache: Dict[str, Dict[str, Any]] = {}

app = FastAPI(title="PJe TRF6 - Consulta Pública")

# ========= Helpers =========

async def selecionar_tipo_documento_trf6(page, tipo: str):
    tipo_str = "CNPJ" if tipo.lower() == "cnpj" else "CPF"
    selector = f"input[name='tipoMascaraDocumento'][onclick*='{tipo_str}']"
    frames = [page.main_frame] + page.frames
    for fr in frames:
        try:
            radios = fr.locator(selector)
            if await radios.count() > 0:
                await radios.first.evaluate("el => el.click()")
                await page.wait_for_timeout(500)
                return
        except:
            continue

async def find_input_trf6(page):
    selector = "[id='fPP:dpDec:documentoParte']"
    frames = [page.main_frame] + page.frames
    for fr in frames:
        try:
            inp = fr.locator(selector)
            if await inp.count() > 0 and await inp.is_visible():
                return fr, inp.first
        except:
            continue
    return None, None

async def wait_spinner_or_delay(page):
    # Aguarda spinner sumir
    candidates = ["[id*='status']", ".ui-widget-overlay", "img[src*='spinner']"]
    await page.wait_for_timeout(500)
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                await loc.wait_for(state="hidden", timeout=20000)
        except:
            pass

async def open_process_popup(page, clickable):
    try:
        async with page.expect_popup(timeout=15000) as pop:
            try:
                await clickable.click(timeout=3000)
            except:
                await clickable.evaluate("el => el.click()")
        popup = await pop.value
        await popup.wait_for_load_state("domcontentloaded")
        return popup
    except PlaywrightTimeoutError:
        return None

async def extract_metadata(popup) -> Dict[str, Optional[str]]:
    try:
        body = await popup.locator("body").inner_text()
    except:
        return {}
    lines = [_norm(ln) for ln in body.replace("\r", "").split("\n") if _norm(ln)]
    
    def find_value(keys: List[str]) -> Optional[str]:
        keys_l = [k.lower() for k in keys]
        for i, ln in enumerate(lines):
            low = ln.lower()
            if any(k in low for k in keys_l):
                parts = re.split(r"[:\-]\s*", ln, maxsplit=1)
                if len(parts) == 2 and parts[1].strip():
                    val = parts[1].strip()
                    if not UNWANTED_RE.search(val): return val
                if i + 1 < len(lines):
                    val = lines[i + 1]
                    if not UNWANTED_RE.search(val): return val
        return None

    return {
        "assunto": find_value(["assunto"]),
        "classe_judicial": find_value(["classe judicial", "classe"]),
        "data_distribuicao": find_value(["distribuição"]),
        "orgao_julgador": find_value(["órgão julgador"]),
        "jurisdicao": find_value(["jurisdição", "comarca"]),
    }

async def extract_movements(popup) -> List[str]:
    # Tenta clicar na aba
    candidates = [
        popup.get_by_role("tab", name=re.compile(r"Movimenta", re.I)),
        popup.locator("text=/Movimenta(ç|c)ões/i"),
    ]
    for c in candidates:
        try:
            if await c.count() > 0 and await c.first.is_visible():
                await c.first.click(timeout=2000)
                await popup.wait_for_timeout(500)
                break
        except: pass

    texts = []
    seen = set()
    selectors = ["tbody[id*='tabelaMovimentacoes'] tr", "table[id*='movimentacao'] tr", ".rich-table-row"]
    for sel in selectors:
        try:
            rows = popup.locator(sel)
            cnt = await rows.count()
            if cnt > 0:
                for i in range(min(cnt, 15)):
                    t = _norm(await rows.nth(i).inner_text())
                    if t and t not in seen and not UNWANTED_RE.search(t):
                        seen.add(t)
                        texts.append(t)
                break
        except: pass
    return texts

async def extract_partes_from_row(link) -> Optional[str]:
    try:
        row = link.locator("xpath=ancestor::tr[1]")
        row_text = await row.inner_text()
        return _norm(row_text)
    except:
        return None

# ========= Scraper principal =========

async def scrape_pje(doc_digits: str, tipo: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "documento": doc_digits,
        "tipo": tipo.upper(),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "processos": [],
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768}
        )
        page = await context.new_page()

        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            
            # 1. Seleciona Tipo
            await selecionar_tipo_documento_trf6(page, tipo)

            # 2. Input
            fr, doc_input = await find_input_trf6(page)
            if not doc_input:
                raise Exception("campo_input_nao_encontrado")

            # 3. Preenche e força foco
            await doc_input.click()
            await doc_input.fill(doc_digits)
            await page.wait_for_timeout(200)

            # 4. Busca Botão de forma global (mais seguro)
            # O ID contém ':', usamos seletor de atributo
            btn_selector = "[id='fPP:searchProcessos']"
            
            # Tenta achar o botão no frame do input ou globalmente
            btn = fr.locator(btn_selector) if fr else page.locator(btn_selector)
            
            clicked = False
            if await btn.count() > 0:
                try:
                    await btn.first.click(timeout=5000)
                    clicked = True
                except:
                    # Fallback JS
                    await btn.first.evaluate("el => el.click()")
                    clicked = True
            
            if not clicked:
                await doc_input.press("Enter")

            # 5. Espera Ativa pelos Resultados (Pooling)
            # Aguarda até 10 segundos aparecer algum link com regex CNJ ou texto de resultado
            start_wait = time.time()
            found_results = False
            
            while (time.time() - start_wait) < 10:
                # Verifica se spinner está rodando e espera
                await wait_spinner_or_delay(page)
                
                # Procura links
                proc_links = page.locator("a").filter(has_text=CNJ_RE)
                count = await proc_links.count()
                
                if count > 0:
                    found_results = True
                    break
                
                # Verifica se apareceu aviso de "Nenhum registro"
                content = await page.content()
                if "nenhum registro" in content.lower() or "não foram encontrados" in content.lower():
                    break
                    
                await page.wait_for_timeout(1000)

            if not found_results:
                # DEBUG: Se não achou nada, retorna o HTML para entendermos
                html_snapshot = await page.content()
                result["mensagem"] = "nenhum_processo_encontrado_no_tempo_limite"
                # Limita tamanho para não explodir o JSON
                result["html_debug"] = html_snapshot[:20000] 
                return result

            # 6. Extração
            proc_links = page.locator("a").filter(has_text=CNJ_RE)
            count = await proc_links.count()
            seen_nums = set()

            for i in range(count):
                link = proc_links.nth(i)
                txt = _norm(await link.inner_text())
                m = CNJ_RE.search(txt)
                if not m: continue
                numero = m.group(0)
                
                if numero in seen_nums: continue
                seen_nums.add(numero)

                partes = await extract_partes_from_row(link)

                popup = await open_process_popup(page, link)
                if popup:
                    meta = await extract_metadata(popup)
                    movs = await extract_movements(popup)
                    result["processos"].append({
                        "numero": numero,
                        "partes_resumo": partes,
                        **meta,
                        "movimentacoes": movs,
                    })
                    await popup.close()
                else:
                    result["processos"].append({
                        "numero": numero,
                        "aviso": "popup_nao_abriu",
                        "resumo": partes
                    })

        except Exception as e:
            result["erro_interno"] = str(e)
            
        finally:
            await browser.close()

    return result

# ========= Endpoints =========

@app.get("/health")
def health():
    return {"ok": True, "target": "TRF6"}

@app.get("/consulta")
async def consulta(
    doc: str = Query(..., description="CPF ou CNPJ apenas números"),
    type: str = Query("cpf", alias="type"),
):
    tipo = type.strip().lower()
    doc_digits = sanitize_doc(doc)
    
    if not doc_digits:
        raise HTTPException(status_code=400, detail="documento_vazio")

    cache_key = f"trf6:{tipo}:{doc_digits}"
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached["ts"]) < CACHE_TTL:
        return cached["data"]

    async with SEMA:
        try:
            # Aumentei timeout para 180s pois o scraping agora espera mais
            data = await asyncio.wait_for(scrape_pje(doc_digits, tipo), timeout=180)
            _cache[cache_key] = {"ts": time.time(), "data": data}
            return data
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="timeout_trf6")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
