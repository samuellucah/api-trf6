import re
import time
import asyncio
import nest_asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, HTTPException
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# Permite loops aninhados (essencial para FastAPI + Playwright)
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
    """Remove tudo que não for número."""
    return re.sub(r"\D+", "", doc or "")

# ===== Concurrency + Cache =====
SEMA = asyncio.Semaphore(1)           # 1 request por vez (Playwright é pesado)
CACHE_TTL = 300                       # 5 minutos
_cache: Dict[str, Dict[str, Any]] = {}

app = FastAPI(title="PJe TRF6 - Consulta Pública")


# ========= Helpers de página =========

async def selecionar_tipo_documento_trf6(page, tipo: str):
    """
    Seleciona CPF ou CNPJ baseado no onclick do HTML fornecido:
    onclick="mascaraDocumento('documentoParte', 'CNPJ')"
    """
    tipo_str = "CNPJ" if tipo.lower() == "cnpj" else "CPF"
    
    # Procura input que tenha o onclick correspondente
    # Ex: input[onclick*='CNPJ']
    selector = f"input[name='tipoMascaraDocumento'][onclick*='{tipo_str}']"

    frames = [page.main_frame] + page.frames
    for fr in frames:
        try:
            radios = fr.locator(selector)
            if await radios.count() > 0:
                # Força o clique via JS para garantir, pois as vezes o label cobre
                await radios.first.evaluate("el => el.click()")
                await page.wait_for_timeout(500)
                return
        except:
            continue

async def find_input_trf6(page):
    """
    Encontra o input específico do TRF6 pelo ID: fPP:dpDec:documentoParte
    Como IDs com ':' precisam ser escapados em CSS, usamos [id='...'] ou escape.
    """
    # Seletor robusto por atributo ID exato
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
    """
    Aguarda spinners do PJe (RichFaces/JSF status).
    """
    # RichFaces status normal
    candidates = [
        "[id*='status']", 
        ".ui-widget-overlay", 
        "img[src*='spinner']",
        "div[id*='submitStatus']" # Comum em JSF/Seam
    ]
    
    # Pequeno delay inicial para dar tempo do spinner aparecer
    await page.wait_for_timeout(500)
    
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                # Espera sumir
                await loc.wait_for(state="hidden", timeout=25000)
        except:
            pass

async def open_process_popup(page, clickable):
    try:
        async with page.expect_popup(timeout=20000) as pop:
            # Tenta clicar. Se houver JS blocking, força via evaluate
            try:
                await clickable.click(timeout=5000)
            except:
                await clickable.evaluate("el => el.click()")
                
        popup = await pop.value
        await popup.wait_for_load_state("domcontentloaded")
        return popup
    except PlaywrightTimeoutError:
        return None

# ... (Funções de extração extract_metadata, extract_movements, extract_partes_from_row 
#      permanecem iguais, pois a estrutura interna do PJe costuma ser padrão) ...

async def try_click_movements_tab(popup):
    candidates = [
        popup.get_by_role("tab", name=re.compile(r"Movimenta", re.I)),
        popup.locator("text=/Movimenta(ç|c)ões/i"),
        popup.locator("div[id*='divMovimentacao']"), # PJe antigo
    ]
    for c in candidates:
        try:
            if await c.count() > 0 and await c.first.is_visible():
                await c.first.click(timeout=3000)
                await popup.wait_for_timeout(800)
                return
        except:
            pass

async def extract_metadata(popup) -> Dict[str, Optional[str]]:
    # Mesma lógica do anterior (genérica para PJe)
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
    await try_click_movements_tab(popup)
    texts = []
    seen = set()
    # Seletores comuns de tabelas de movimentação PJe
    selectors = ["tbody[id*='tabelaMovimentacoes'] tr", "table[id*='movimentacao'] tr", ".rich-table-row"]
    
    for sel in selectors:
        try:
            rows = popup.locator(sel)
            cnt = await rows.count()
            if cnt > 0:
                for i in range(min(cnt, 20)):
                    t = _norm(await rows.nth(i).inner_text())
                    if t and t not in seen and not UNWANTED_RE.search(t):
                        seen.add(t)
                        texts.append(t)
                break
        except:
            pass
    return texts

async def extract_partes_from_row(link) -> Optional[str]:
    try:
        # PJe 1.x TRF costuma ser tabela richfaces
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
            await page.goto(URL, wait_until="networkidle", timeout=60000)
            
            # 1. Seleciona o Radio Button (CPF/CNPJ)
            await selecionar_tipo_documento_trf6(page, tipo)

            # 2. Encontra o Input pelo ID específico
            fr, doc_input = await find_input_trf6(page)
            if not doc_input:
                raise Exception("campo_input_nao_encontrado")

            # 3. Preenche
            await doc_input.click()
            await doc_input.fill(doc_digits)
            
            # 4. Clica no Botão Pesquisar (ID: fPP:searchProcessos)
            # O ID contém ':', então usamos seletor de atributo para evitar erro de sintaxe CSS ou escape
            search_btn = fr.locator("[id='fPP:searchProcessos']")
            if await search_btn.count() > 0:
                await search_btn.click(timeout=30000)
            else:
                await doc_input.press("Enter")

            # Aguarda carregamento
            await wait_spinner_or_delay(page)

            # 5. Verifica Resultados
            # TRF6/PJe antigo geralmente mostra tabela com classe rich-table ou id processTable
            # Vamos procurar links que batam com o Regex do CNJ
            proc_links = page.locator("a").filter(has_text=CNJ_RE)
            
            # Fallback: Se não achar links diretos, procura em células da tabela
            if await proc_links.count() == 0:
                # Procura texto na página para depuração
                content = await page.content()
                if "Não foram encontrados dados" in content:
                    result["mensagem"] = "nenhum_processo_encontrado"
                    return result
            
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

                # Abre Popup
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
                    # Tenta pegar dados da linha sem abrir popup se falhar
                    result["processos"].append({
                        "numero": numero,
                        "aviso": "popup_nao_abriu",
                        "resumo": partes
                    })

        except Exception as e:
            result["erro_interno"] = str(e)
            # Tira print se der erro para debug (opcional, requer volume montado)
            # await page.screenshot(path="error_trf6.png")
            
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
    
    # Check Cache
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached["ts"]) < CACHE_TTL:
        return cached["data"]

    async with SEMA:
        try:
            data = await asyncio.wait_for(scrape_pje(doc_digits, tipo), timeout=120)
            _cache[cache_key] = {"ts": time.time(), "data": data}
            return data
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="timeout_trf6")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))