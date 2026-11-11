"""Script de geração do PDF Executivo do Lobo AI Sales Agent (Estratégia, Operação e Planos).

Utiliza ReportLab 5 para produzir um documento A4 com design editorial moderno,
cabeçalhos, rodapés com numeração de páginas dinâmica, tabelas zebradas e caixas de destaque.
"""

from __future__ import annotations

import sys
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

OUTPUT_PDF = Path(__file__).parent / "LOBO_AI_SALES_AGENT_ESTRATEGIA_E_PRODUTO.pdf"


class NumberedCanvas(canvas.Canvas):
    """Canvas com duas passadas para calcular o total real de páginas no rodapé."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count: int):
        self.saveState()
        self.setFont("Helvetica-Bold", 8)
        self.setFillColor(colors.HexColor("#0284c7"))  # Teal / Cyan brand

        # Cabeçalho (a partir da página 2)
        if self._pageNumber > 1:
            self.drawRightString(A4[0] - 2.0 * cm, A4[1] - 1.4 * cm, "LOBOADS  /  ESTRATÉGIA DE PRODUTO")
            self.setStrokeColor(colors.HexColor("#e2e8f0"))
            self.setLineWidth(0.5)
            self.line(2.0 * cm, A4[1] - 1.6 * cm, A4[0] - 2.0 * cm, A4[1] - 1.6 * cm)

        # Rodapé em todas as páginas
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748b"))
        footer_text = f"LOBO AI SALES AGENT • DOCUMENTO ESTRATÉGICO  |  Página {self._pageNumber} de {page_count}"
        self.drawCentredString(A4[0] / 2.0, 1.2 * cm, footer_text)

        self.setStrokeColor(colors.HexColor("#e2e8f0"))
        self.setLineWidth(0.5)
        self.line(2.0 * cm, 1.6 * cm, A4[0] - 2.0 * cm, 1.6 * cm)

        self.restoreState()


def create_callout(
    title: str, text: str, styles: dict[str, ParagraphStyle], bg_color="#f0f9ff", border_color="#0284c7"
) -> Table:
    """Cria uma caixa destacada (card de citação / regra de ouro)."""
    title_p = Paragraph(f"<b>{title.upper()}</b>", styles["CalloutTitle"])
    content_p = Paragraph(text, styles["CalloutBody"])
    card_table = Table([[title_p], [content_p]], colWidths=[17.0 * cm])
    card_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(bg_color)),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor(border_color)),
                ("LINELEFT", (0, 0), (0, -1), 3.5, colors.HexColor(border_color)),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return card_table


def build_pdf():
    doc = SimpleDocTemplate(
        str(OUTPUT_PDF),
        pagesize=A4,
        leftMargin=2.0 * cm,
        rightMargin=2.0 * cm,
        topMargin=2.2 * cm,
        bottomMargin=2.2 * cm,
    )

    styles = getSampleStyleSheet()

    # Tipografias personalizadas
    styles.add(ParagraphStyle("CoverSuper", fontName="Helvetica-Bold", fontSize=9, textColor=colors.HexColor("#0284c7"), leading=12, spaceAfter=4))
    styles.add(ParagraphStyle("CoverTitle", fontName="Helvetica-Bold", fontSize=26, textColor=colors.HexColor("#0f172a"), leading=30, spaceAfter=8))
    styles.add(ParagraphStyle("CoverSub", fontName="Helvetica", fontSize=13, textColor=colors.HexColor("#475569"), leading=17, spaceAfter=14))
    styles.add(ParagraphStyle("DocMeta", fontName="Helvetica", fontSize=8.5, textColor=colors.HexColor("#64748b"), leading=12, spaceAfter=14))

    styles.add(ParagraphStyle("Heading1Custom", fontName="Helvetica-Bold", fontSize=15, textColor=colors.HexColor("#0f172a"), leading=19, spaceBefore=14, spaceAfter=6, keepWithNext=True))
    styles.add(ParagraphStyle("Heading2Custom", fontName="Helvetica-Bold", fontSize=11.5, textColor=colors.HexColor("#1e293b"), leading=15, spaceBefore=10, spaceAfter=4, keepWithNext=True))
    styles.add(ParagraphStyle("Heading3Custom", fontName="Helvetica-Bold", fontSize=9.5, textColor=colors.HexColor("#334155"), leading=13, spaceBefore=6, spaceAfter=2, keepWithNext=True))

    styles.add(ParagraphStyle("BodyCustom", fontName="Helvetica", fontSize=9, textColor=colors.HexColor("#334155"), leading=13.5, spaceAfter=6))
    styles.add(ParagraphStyle("BulletCustom", fontName="Helvetica", fontSize=8.8, textColor=colors.HexColor("#334155"), leading=13, leftIndent=12, firstLineIndent=-8, spaceAfter=3))

    styles.add(ParagraphStyle("CalloutTitle", fontName="Helvetica-Bold", fontSize=9.5, textColor=colors.HexColor("#0369a1"), leading=13, spaceAfter=2))
    styles.add(ParagraphStyle("CalloutBody", fontName="Helvetica", fontSize=8.8, textColor=colors.HexColor("#0f172a"), leading=13))

    styles.add(ParagraphStyle("TableHeader", fontName="Helvetica-Bold", fontSize=8.5, textColor=colors.white, leading=11, alignment=1))
    styles.add(ParagraphStyle("TableCell", fontName="Helvetica", fontSize=8, textColor=colors.HexColor("#1e293b"), leading=11))
    styles.add(ParagraphStyle("TableCellBold", fontName="Helvetica-Bold", fontSize=8, textColor=colors.HexColor("#0f172a"), leading=11))
    styles.add(ParagraphStyle("TableCellCenter", fontName="Helvetica", fontSize=8, textColor=colors.HexColor("#1e293b"), leading=11, alignment=1))

    story = []

    # =========================================================================
    # CAPA / CABEÇALHO DO DOCUMENTO
    # =========================================================================
    story.append(Paragraph("LOBOADS  /  NOVO PRODUTO", styles["CoverSuper"]))
    story.append(Paragraph("LOBO AI SALES AGENT", styles["CoverTitle"]))
    story.append(Paragraph("Estratégia de negócio, operação comercial e modelo de monetização", styles["CoverSub"]))

    direcao_box = create_callout(
        "DIREÇÃO RECOMENDADA",
        "Construir uma plataforma própria e reutilizável, começar a comercializá-la como <b>serviço gerenciado</b> (implantação + mensalidade + consumo) e evoluir para um <b>SaaS multiempresa</b> conforme a operação e o produto amadurecerem.",
        styles,
        bg_color="#f0fdf4",
        border_color="#16a34a",
    )
    story.append(direcao_box)
    story.append(Spacer(1, 10))

    meta_text = "<b>Documento estratégico para avaliação, operação e planejamento.</b><br/>Data de referência: Novembro de 2026 | Versão 1.0<br/><i>Este documento NÃO é uma especificação técnica de código. É a visão de negócio, experiência de uso final e modelo financeiro proposto.</i>"
    story.append(Paragraph(meta_text, styles["DocMeta"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cbd5e1"), spaceAfter=12))

    # =========================================================================
    # 1. RESUMO EXECUTIVO & TESE CENTRAL
    # =========================================================================
    story.append(Paragraph("Resumo executivo", styles["Heading1Custom"]))
    story.append(Paragraph(
        "A oportunidade é transformar o atendimento com inteligência artificial em um <b>produto comercial proprietário da LoboAds</b>, capaz de atender diferentes nichos de mercado (varejo, clínicas, prestadores de serviços e indústrias locais) sem precisar reconstruir a solução do zero a cada novo contrato.",
        styles["BodyCustom"]
    ))
    story.append(Paragraph(
        "A recomendação estratégica é <b>não começar como um SaaS self-service aberto</b>, onde o cliente cria conta sozinho. O caminho mais lucrativo e seguro é atuar como <b>serviço gerenciado de alta performance</b>: a LoboAds mapeia o processo, personaliza o catálogo, configura as regras de vendas, monitora e garante o resultado; o empresário recebe os clientes atendidos e qualificados no WhatsApp com agendamentos e vendas geradas.",
        styles["BodyCustom"]
    ))

    tese_box = create_callout(
        "A TESE CENTRAL",
        "<b>Não vender um robô, um script ou uma ferramenta de automação.</b> Vender uma operação de atendimento e conversão completa que conecta anúncios de tráfego pago, acolhimento no WhatsApp, qualificação comercial, catálogo, agendamento de visitas e fechamento de vendas.",
        styles,
        bg_color="#eff6ff",
        border_color="#2563eb",
    )
    story.append(tese_box)
    story.append(Spacer(1, 8))

    story.append(Paragraph("Decisões Principais de Produto:", styles["Heading2Custom"]))
    story.append(Paragraph("• <b>Produto:</b> Lobo AI Sales Agent — motor comercial de atendimento e conversão com IA para múltiplas empresas.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Modelo Comercial:</b> Implantação (setup) + Mensalidade recorrente (MRR) + Franquia de consumo de mensagens.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Abordagem Humana:</b> Eliminação total de jargões de robô (\"sou uma IA\", \"digite 1\"), substituídos por cadência de vendedor real, áudios naturais e escalação elegante para gerentes.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Multimodalidade Completa:</b> Ouve e compreende qualquer áudio enviado pelo cliente no WhatsApp e quebra respostas longas em balões confortáveis.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Estratégia de Crescimento:</b> Validar com 3 a 5 clientes-piloto em Belém/PA antes de abrir comercialização em escala regional.", styles["BulletCustom"]))

    story.append(Spacer(1, 10))

    # =========================================================================
    # 2. A OPORTUNIDADE DE NEGÓCIO E A DOR REAL DO EMPRESÁRIO
    # =========================================================================
    story.append(Paragraph("1. A oportunidade de negócio e a dor do mercado", styles["Heading1Custom"]))
    story.append(Paragraph(
        "O Lobo Sales Agent resolve a maior dor de quem investe em marketing: <b>o cemitério de leads no WhatsApp</b>. Empresas investem de R$ 2.000 a R$ 20.000 por mês em anúncios no Meta Ads e Google Ads, gerando centenas de contatos. Porém, até 70% dessas oportunidades são jogadas no lixo por falhas humanas previsíveis:",
        styles["BodyCustom"]
    ))

    story.append(Paragraph("• <b>Demora Mortal no Primeiro Contato:</b> Leads não respondidos nos primeiros 5 minutos perdem até 80% do interesse de compra.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>O Apagão Noturno e de Finais de Semana:</b> Mais de 40% das pessoas clicam em anúncios à noite e nos finais de semana — exatamente quando as lojas físicas estão fechadas e os funcionários não respondem.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Atendimento Frio e Sem Técnica:</b> Atendentes sobrecarregados respondem de forma monossilábica (\"Tenho sim\", \"Custa 1.200\"), sem fazer perguntas de qualificação nem conduzir o cliente até o próximo passo.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>A Rejeição aos Robôs Antigos:</b> Chatbots baseados em botões ou árvores de decisão (\"digite 1 para financeiro\") irritam o público moderno, não aceitam áudios e geram abandono imediato.", styles["BulletCustom"]))

    story.append(Spacer(1, 10))

    # =========================================================================
    # 3. O SISTEMA HOJE: CAPACIDADES OPERACIONAIS COMPROVADAS
    # =========================================================================
    story.append(Paragraph("2. O sistema hoje: Como funciona na prática", styles["Heading1Custom"]))
    story.append(Paragraph(
        "O Lobo Sales Agent já está em operação funcional, validado com dados reais e testes de estresse comercial. Suas principais capacidades operacionais incluem:",
        styles["BodyCustom"]
    ))

    story.append(Paragraph("A. Experiência de Conversa do Lead (WhatsApp):", styles["Heading2Custom"]))
    story.append(Paragraph("• <b>Compreensão Nativa de Áudios:</b> O cliente pode gravar áudios de 5 a 60 segundos com sotaques, gírias regionais e ruídos de fundo. O sistema ouve, transcreve e responde ao conteúdo sem pedir para digitar.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Cadência Humana & Mensagens Picotadas (Split Messages):</b> Em vez de mandar blocos gigantescos de texto (\"textões\"), a IA envia confirmação de leitura (dois tracinhos azuis), simula digitação e entrega as respostas divididas em 2 a 3 balões confortáveis.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Acolhimento Vendedor (SDR / Closer):</b> Nunca fala como máquina (\"Vi que você veio pelo anúncio\"). Acolhe como consultor local da loja, descobre as preferências do cliente e apresenta soluções compatíveis.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Catálogo e Preços Atualizados:</b> Conhece produtos, variações (solteiro, casal, queen, king), tecnologias (molas ensacadas, ortopédico, espuma D33) e condições de pagamento (Pix à vista ou cartão sem juros).", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Inteligência Geográfica Local:</b> Reconhece bairros (Tapanã, Marambaia, Umarizal, Cidade Nova, etc.), calcula regras de frete grátis na Região Metropolitana e convida para testes de conforto no showroom físico.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Escalação Hierárquica sem 'Cara de Robô':</b> Se o cliente pede desconto extraordinário, a IA diz: <i>\"Vou falar diretamente com o meu gerente comercial agora para ver se ele autoriza essa condição especial para você, combinado?\"</i>. Se o atendente humano digitar no celular, a IA se desativa em silêncio.", styles["BulletCustom"]))

    story.append(Spacer(1, 6))
    story.append(Paragraph("B. Painel Administrativo da LoboAds:", styles["Heading2Custom"]))
    story.append(Paragraph("• <b>Visão Geral & Sandbox:</b> Trava de segurança para testar clientes novos em ambiente seguro com liberação por palavra-chave (<code>/lobo</code>) e lista de telefones autorizados.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Histórico & Auditoria:</b> Registro completo de contatos, etapas do funil comercial e transcrições de conversas.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Playground Interativo:</b> Simulador para testar novos catálogos e respostas antes de colocar no ar.", styles["BulletCustom"]))

    # story.append(PageBreak())

    # =========================================================================
    # 4. TABELA DE PLANOS, VALORES E MONETIZAÇÃO
    # =========================================================================
    story.append(Paragraph("3. Modelo comercial, planos e monetização", styles["Heading1Custom"]))
    story.append(Paragraph(
        "O modelo financeiro da LoboAds é composto por 3 fontes de receita complementares: <b>Taxa de Implantação (Setup)</b>, <b>Mensalidade Recorrente da Operação (MRR)</b> e <b>Consumo Excedente de Conversas</b>.",
        styles["BodyCustom"]
    ))

    # Tabela de Planos
    planos_data = [
        [
            Paragraph("<b>Componente</b>", styles["TableHeader"]),
            Paragraph("<b>Plano Essencial</b>", styles["TableHeader"]),
            Paragraph("<b>Plano Pro Vendas<br/><i>(Mais Vendido)</i></b>", styles["TableHeader"]),
            Paragraph("<b>Plano Escala Enterprise</b>", styles["TableHeader"]),
        ],
        [
            Paragraph("<b>Público Ideal</b>", styles["TableCellBold"]),
            Paragraph("Consultórios, prestadores, pequenas lojas", styles["TableCell"]),
            Paragraph("Lojas de varejo, colchões, piscinas, clínicas", styles["TableCell"]),
            Paragraph("Redes com filiais, grandes marcas, franquias", styles["TableCell"]),
        ],
        [
            Paragraph("<b>Taxa de Implantação</b>", styles["TableCellBold"]),
            Paragraph("<b>R$ 2.500,00</b> <i>(única)</i>", styles["TableCellCenter"]),
            Paragraph("<b>R$ 4.500,00</b> <i>(única)</i>", styles["TableCellCenter"]),
            Paragraph("<b>R$ 7.500 a R$ 12.000</b>", styles["TableCellCenter"]),
        ],
        [
            Paragraph("<b>Mensalidade Recorrente</b>", styles["TableCellBold"]),
            Paragraph("<b>R$ 790,00 / mês</b>", styles["TableCellCenter"]),
            Paragraph("<b>R$ 1.690,00 / mês</b>", styles["TableCellCenter"]),
            Paragraph("<b>R$ 2.990,00 / mês</b>", styles["TableCellCenter"]),
        ],
        [
            Paragraph("<b>Canais / WhatsApp</b>", styles["TableCellBold"]),
            Paragraph("1 número oficial", styles["TableCellCenter"]),
            Paragraph("1 a 2 números", styles["TableCellCenter"]),
            Paragraph("Até 4 números / setores", styles["TableCellCenter"]),
        ],
        [
            Paragraph("<b>Conversas Inclusas</b>", styles["TableCellBold"]),
            Paragraph("Até 800 conversas/mês", styles["TableCellCenter"]),
            Paragraph("Até 2.500 conversas/mês", styles["TableCellCenter"]),
            Paragraph("Até 6.000 conversas/mês", styles["TableCellCenter"]),
        ],
        [
            Paragraph("<b>Compreensão de Áudios</b>", styles["TableCellBold"]),
            Paragraph("✅ Ilimitada", styles["TableCellCenter"]),
            Paragraph("✅ Ilimitada", styles["TableCellCenter"]),
            Paragraph("✅ Ilimitada", styles["TableCellCenter"]),
        ],
        [
            Paragraph("<b>Resposta em Áudio Clonado</b>", styles["TableCellBold"]),
            Paragraph("❌ (Apenas texto)", styles["TableCellCenter"]),
            Paragraph("✅ Incluso (voz personalizada)", styles["TableCellCenter"]),
            Paragraph("✅ Incluso (múltiplas vozes)", styles["TableCellCenter"]),
        ],
        [
            Paragraph("<b>Catálogo & Preços</b>", styles["TableCellBold"]),
            Paragraph("Até 15 itens principais", styles["TableCellCenter"]),
            Paragraph("Catálogo completo ilimitado", styles["TableCellCenter"]),
            Paragraph("Integrado a ERP / Estoque", styles["TableCellCenter"]),
        ],
        [
            Paragraph("<b>Inteligência de Frete</b>", styles["TableCellBold"]),
            Paragraph("Básico (cidade matriz)", styles["TableCellCenter"]),
            Paragraph("Completo por bairros e km", styles["TableCellCenter"]),
            Paragraph("Multi-cidades e filiais", styles["TableCellCenter"]),
        ],
        [
            Paragraph("<b>Régua de Follow-up Ativo</b>", styles["TableCellBold"]),
            Paragraph("❌", styles["TableCellCenter"]),
            Paragraph("✅ Follow-up de 2h e 24h", styles["TableCellCenter"]),
            Paragraph("✅ Régua avançada multietapas", styles["TableCellCenter"]),
        ],
        [
            Paragraph("<b>Integração de CRM</b>", styles["TableCellBold"]),
            Paragraph("ChatCRM básico", styles["TableCellCenter"]),
            Paragraph("ChatCRM / GHL completo", styles["TableCellCenter"]),
            Paragraph("CRM corporativo dedicado", styles["TableCellCenter"]),
        ],
        [
            Paragraph("<b>Relatórios Executivos</b>", styles["TableCellBold"]),
            Paragraph("Mensal simples", styles["TableCellCenter"]),
            Paragraph("Semanal em PDF no WhatsApp", styles["TableCellCenter"]),
            Paragraph("Dashboard ao vivo + reunião", styles["TableCellCenter"]),
        ],
        [
            Paragraph("<b>Conversa Excedente</b>", styles["TableCellBold"]),
            Paragraph("R$ 0,40 / conversa", styles["TableCellCenter"]),
            Paragraph("R$ 0,30 / conversa", styles["TableCellCenter"]),
            Paragraph("R$ 0,20 / conversa", styles["TableCellCenter"]),
        ],
    ]

    table_planos = Table(planos_data, colWidths=[4.2 * cm, 4.1 * cm, 4.4 * cm, 4.3 * cm])
    table_planos.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(table_planos)
    story.append(Spacer(1, 10))

    # O que está incluso no setup
    story.append(Paragraph("O que deve estar incluso na Taxa de Implantação (Setup):", styles["Heading2Custom"]))
    story.append(Paragraph("• <b>Mapeamento do Funil Comercial:</b> Entrevista de diagnóstico para extrair dúvidas frequentes, principais objeções de compra e diferenciais do negócio.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Estruturação do Conhecimento:</b> Cadastro de produtos, fotos, especificações, tabela de preços, condições parceladas e políticas de frete.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Calibração do Tom de Voz:</b> Ajuste fino de vocabulário, acolhimento regional e limites éticos de atuação.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Conexão Segura com WhatsApp e CRM:</b> Setup técnico, verificação de webhooks e garantia de estabilidade.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Simulação e Testes em Sandbox:</b> Validação prévia com dezenas de mensagens reais antes do lançamento público.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Treinamento da Equipe Humana da Loja:</b> Capacitação dos atendentes para trabalhar em sintonia com o agente.", styles["BulletCustom"]))

    story.append(Spacer(1, 10))

    # Simulador de ROI
    roi_box = create_callout(
        "O RETORNO SOBRE O INVESTIMENTO (ROI) PARA O CLIENTE",
        "<b>Exemplo Prático (Loja de Colchões ou Piscinas):</b><br/>"
        "• Ticket médio de compra: <b>R$ 2.000,00</b><br/>"
        "• Leads recebidos por mês: <b>300 pessoas</b><br/>"
        "• Leads perdidos por demora ou fora de hora: <b>~45 pessoas (15%)</b><br/>"
        "• Faturamento que a empresa deixa na mesa todo mês: <b>R$ 90.000,00</b><br/><br/>"
        "Se o <b>Lobo Sales Agent</b> recuperar <b>apenas 2 dessas 45 vendas perdidas</b>, ele adiciona <b>R$ 4.000,00</b> de faturamento novo no primeiro mês.<br/>"
        "O cliente paga a mensalidade do Plano Pro (R$ 1.690) e ainda sobram <b>R$ 2.310 de lucro limpo</b> no caixa da empresa. O sistema se paga com folga absoluta!",
        styles,
        bg_color="#f0fdf4",
        border_color="#16a34a",
    )
    story.append(roi_box)
    story.append(Spacer(1, 10))

    # Projeção de MRR para a LoboAds
    story.append(Paragraph("Projeção Financeira de Recorrência para a LoboAds (MRR):", styles["Heading2Custom"]))
    mrr_data = [
        [Paragraph("<b>Cenário de Clientes</b>", styles["TableHeader"]), Paragraph("<b>Receita Setup (Média R$ 4.500)</b>", styles["TableHeader"]), Paragraph("<b>MRR Mensal (Média R$ 1.690)</b>", styles["TableHeader"]), Paragraph("<b>Receita Anual Projetada</b>", styles["TableHeader"])],
        [Paragraph("<b>5 Clientes Piloto</b>", styles["TableCellBold"]), Paragraph("R$ 22.500,00", styles["TableCellCenter"]), Paragraph("<b>R$ 8.450,00 / mês</b>", styles["TableCellCenter"]), Paragraph("R$ 123.900,00 / ano", styles["TableCellCenter"])],
        [Paragraph("<b>15 Clientes Ativos</b>", styles["TableCellBold"]), Paragraph("R$ 67.500,00", styles["TableCellCenter"]), Paragraph("<b>R$ 25.350,00 / mês</b>", styles["TableCellCenter"]), Paragraph("R$ 371.700,00 / ano", styles["TableCellCenter"])],
        [Paragraph("<b>30 Clientes Ativos</b>", styles["TableCellBold"]), Paragraph("R$ 135.000,00", styles["TableCellCenter"]), Paragraph("<b>R$ 50.700,00 / mês</b>", styles["TableCellCenter"]), Paragraph("R$ 743.400,00 / ano", styles["TableCellCenter"])],
    ]
    table_mrr = Table(mrr_data, colWidths=[4.0 * cm, 4.3 * cm, 4.4 * cm, 4.3 * cm])
    table_mrr.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(table_mrr)

    # story.append(PageBreak())

    # =========================================================================
    # 5. O QUE FALTA E COMO PRETENDEMOS DEIXAR (ROADMAP)
    # =========================================================================
    story.append(Paragraph("4. O que falta e como pretendemos deixar (Roadmap)", styles["Heading1Custom"]))
    story.append(Paragraph(
        "Para consolidar a liderança no mercado regional e preparar a transição para um produto escalável, o roteiro de evolução foi dividido em três fases claras:",
        styles["BodyCustom"]
    ))

    story.append(Paragraph("Fase 1 — Consolidação Atual (Primeiros 3–5 Clientes Piloto):", styles["Heading2Custom"]))
    story.append(Paragraph("• Operação estável no WhatsApp com textos divididos, compreensão de áudio e catálogo de produtos.", styles["BulletCustom"]))
    story.append(Paragraph("• Escalação automática para gerentes e silenciamento quando o atendente físico assume a conversa.", styles["BulletCustom"]))
    story.append(Paragraph("• Validação em clientes reais da carteira da LoboAds (Colchões, Piscinas, Serviços e Clínicas).", styles["BulletCustom"]))

    story.append(Spacer(1, 6))
    story.append(Paragraph("Fase 2 — Hiper-Humanização e Conversão Agressiva (Próximos 60 Dias):", styles["Heading2Custom"]))
    story.append(Paragraph("• <b>Respostas em Áudio com Voz Clonada:</b> A IA responderá por notas de voz com o timbre e entonação de uma atendente ou do gerente da loja. Em compras de alto valor (colchões, carros, piscinas), ouvir uma voz natural quebra a última barreira de desconfiança do comprador.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Régua de Follow-up Ativo Inteligente:</b> Recuperação automática de contatos que pararam de responder após receber preços (mensagens após 2h, 24h e 72h com ganchos comerciais de estoque e frete).", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Fechamento Imediato via Pix no WhatsApp:</b> Emissão do código Pix Copia e Cola e QR Code dentro da conversa para garantir reservas de produto na hora.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Relatório Executivo Semanal em PDF:</b> Envio direto no WhatsApp do empresário toda segunda-feira de manhã com número de leads atendidos, produtos mais procurados e volume de vendas encaminhadas.", styles["BulletCustom"]))

    story.append(Spacer(1, 6))
    story.append(Paragraph("Fase 3 — Plataforma Multiempresa & SaaS Comercial:", styles["Heading2Custom"]))
    story.append(Paragraph("• <b>Portal do Lojista:</b> Painel simplificado para o próprio empresário cadastrar novos produtos, promoções e alterar preços sem depender da agência.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Canais Unificados:</b> Extensão do atendimento inteligente para o Instagram Direct e chat do site comercial.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Onboarding Automatizado:</b> Importação de catálogos via planilha Excel/CSV e contratação self-service.", styles["BulletCustom"]))

    story.append(Spacer(1, 10))

    # =========================================================================
    # 6. GOVERNANÇA, SEGURANÇA E PROPRIEDADE DOS ATIVOS
    # =========================================================================
    story.append(Paragraph("5. Governança, segurança e propriedade", styles["Heading1Custom"]))
    story.append(Paragraph("• <b>Propriedade dos Ativos:</b> O número de WhatsApp, os contatos e os dados de clientes permanecem sempre sob posse exclusiva da empresa contratante. A tecnologia do agente e a orquestração comercial são propriedade intelectual da LoboAds.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Prevenção de Alucinações:</b> A IA é proibida de inventar preços, estoques ou promessas fora das regras validadas. Em dúvidas complexas, ela sempre recorre à validação do gerente responsável.", styles["BulletCustom"]))
    story.append(Paragraph("• <b>Privacidade de Dados (LGPD):</b> Dados sensíveis de clientes são minimizados e nunca compartilhados entre diferentes empresas da carteira.", styles["BulletCustom"]))

    story.append(Spacer(1, 12))

    conclusao_box = create_callout(
        "CONCLUSÃO & RECOMENDAÇÃO FINAL",
        "O <b>Lobo AI Sales Agent</b> representa o próximo grande pilar de crescimento da LoboAds: a evolução de uma agência de tráfego pago tradicional para uma <b>empresa de tecnologia e aceleração de vendas</b>.<br/>"
        "Com uma oferta turn-key focada na recuperação de faturamento perdido, o produto oferece <b>retorno imediato para o cliente</b> e constrói uma <b>receita recorrente previsível com margem acima de 85% para a agência</b>.",
        styles,
        bg_color="#eff6ff",
        border_color="#1d4ed8",
    )
    story.append(conclusao_box)

    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"PDF gerado com sucesso em: {OUTPUT_PDF}")


if __name__ == "__main__":
    build_pdf()
