// Package chess يقابل plugins/chess.py: endpoint POST /process_move.
//
// نسخة Go خالصة 100% — لا تبعية Node بعد الآن. منطق الشطرنج (توليد
// نقلات قانونية، كش/كش مات/تعادل، FEN) مكتوب هنا مباشرة، والرد الآلي
// (bot) يُنفَّذ عبر استدعاء ثنائي Stockfish الخارجي كعملية فرعية عبر
// بروتوكول UCI القياسي (بنفس أسلوب استدعاء ffmpeg في plugins/sub) —
// وليس عبر Node/chess.js كما في النسخة السابقة من هذا الملف.
//
// ⚠️ فرق سلوكي متعمّد عن نسخة Node القديمة: حقل "image_base64" في الرد
// كان يحتوي PNG حقيقياً (مُولَّد عبر sharp في Node)، بينما هنا هو base64
// لـ SVG خام (لا مكافئ ناضج قابل للتحميل لتحويل SVG→PNG في Go ضمن قيود
// شبكة بيئة التطوير هذه). أي عميل يعرض الصورة مباشرة كـ
// data:image/svg+xml;base64,... بدل image/png سيعمل بلا مشاكل؛ أي عميل
// يفترض PNG صراحة (مثلاً يحفظها بامتداد .png) يحتاج تحديث.
//
// **متطلب نشر جديد:** يحتاج ثنائي `stockfish` مثبَّتاً في مسار PATH (أو
// عبر STOCKFISH_PATH) في صورة Docker النهائية — راجع الـ Dockerfile.
package chess

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"sunkenbot/internal/httpx"
	"sunkenbot/internal/plugins"
)

const Description = "بوت الشطرنج — تحليل الحركات والرد (Go خالص + محرّك Stockfish عبر UCI)"

// DefaultDifficulty: مستوى Stockfish الافتراضي (Skill Level 0-20) عند
// عدم إرسال قيمة صالحة من العميل.
const DefaultDifficulty = 10

// ─── طبقة الخدمة (عقد plugins.Service) ──────────────────────────────

type Service struct{}

func New() *Service { return &Service{} }

func (s *Service) Name() string { return "chess" }

func (s *Service) Routes() []plugins.Route {
	return []plugins.Route{
		{Method: "POST", Pattern: "/process_move", Handler: httpx.Handle(s.handleProcessMove)},
	}
}

// ─── أنواع الطلب/الرد (نفس البروتوكول الذي كان يتحدَّث به سكربت Node) ─

type moveRequest struct {
	FEN        string `json:"fen"`
	Move       string `json:"move"`
	BotMode    bool   `json:"bot_mode"`
	Difficulty int    `json:"difficulty"`
}

type moveResponse struct {
	NewFEN           string `json:"new_fen"`
	ImageBase64      string `json:"image_base64"`
	GameOver         bool   `json:"game_over"`
	Winner           string `json:"winner,omitempty"`
	IllegalMoveError string `json:"illegal_move_error,omitempty"`
}

// stockfishPath: يمكن تجاوزه عبر STOCKFISH_PATH لو نُشر الثنائي في مكان
// مختلف عن الافتراضي (المتوقَّع أن يكون متاحاً عبر PATH بعد apt install).
func stockfishPath() string {
	if p := os.Getenv("STOCKFISH_PATH"); p != "" {
		return p
	}
	return "stockfish"
}

// ─── HTTP handler ──────────────────────────────────────────────────

func (s *Service) handleProcessMove(r *http.Request) (moveResponse, error) {
	var req moveRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		return moveResponse{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"detail": "طلب غير صالح: " + err.Error()},
		}
	}
	// الصعوبة الآن هي مستوى Stockfish UCI "Skill Level" مباشرة (0-20)،
	// وليست مقياساً وسيطاً 1-5 كما كان سابقاً. بما أن صفر قيمة صالحة
	// فعلياً (أضعف مستوى)، لا يمكن استخدامه كقيمة استشعار "غير مُرسَل"؛
	// العميل (chess.js) يُرسِل دائماً قيمة صريحة، وهنا فقط نضبط أي قيمة
	// خارج المجال المسموح 0-20 (بما فيها القيم السالبة الفعلية).
	if req.Difficulty < 0 {
		req.Difficulty = DefaultDifficulty
	}
	if req.Difficulty > 20 {
		req.Difficulty = 20
	}

	board, err := fenToBoard(req.FEN)
	if err != nil {
		return moveResponse{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"detail": "Invalid FEN string: " + err.Error()},
		}
	}

	// تطبيق نقلة الإنسان (إن وُجدت). أي خطأ هنا لا يُعتبر خطأ HTTP —
	// يُرجَع في illegal_move_error مع الـ FEN الأصلي بدون تغيير، تماماً
	// كما كان يفعل سكربت Node.
	if req.Move != "" {
		if err := board.ApplyMove(req.Move); err != nil {
			return moveResponse{
				NewFEN:           req.FEN,
				ImageBase64:      svgBase64(board, ""),
				GameOver:         false,
				IllegalMoveError: err.Error(),
			}, nil
		}
	}

	lastMove := req.Move
	over, winner := board.gameStatus()

	// رد الآلة: فقط لو ما انتهت اللعبة بعد وbot_mode مفعّل.
	if !over && req.BotMode {
		ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
		defer cancel()

		best, err := stockfishBestMove(ctx, board.toFEN(), req.Difficulty)
		if err != nil {
			return moveResponse{}, &httpx.HTTPError{
				Code: http.StatusInternalServerError,
				Body: map[string]any{"detail": truncate(err.Error(), 300)},
			}
		}
		if err := board.ApplyMove(best); err != nil {
			return moveResponse{}, &httpx.HTTPError{
				Code: http.StatusInternalServerError,
				Body: map[string]any{"detail": "نقلة غير صالحة عادت من Stockfish: " + err.Error()},
			}
		}
		lastMove = best
		over, winner = board.gameStatus()
	}

	return moveResponse{
		NewFEN:      board.toFEN(),
		ImageBase64: svgBase64(board, lastMove),
		GameOver:    over,
		Winner:      winner,
	}, nil
}

func svgBase64(b *Board, lastMove string) string {
	svg := boardToSVG(b, lastMove)
	return base64.StdEncoding.EncodeToString([]byte(svg))
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// ─── حالة نهاية اللعبة (كش مات/تعادل) ──────────────────────────────

// gameStatus يقابل gameOver() في النسخة التفاعلية الأصلية، لكن بلا
// طباعة على الطرفية، ويُرجع اسم الفائز جاهزاً للـ JSON.
func (b *Board) gameStatus() (over bool, winner string) {
	if len(b.LegalMoves()) > 0 {
		return false, ""
	}
	if b.inCheck() {
		// الطرف الذي عليه الدور الآن هو من انكشف ملكه، أي الطرف الآخر
		// هو من حقق كش مات.
		if b.Turn {
			return true, "أسود"
		}
		return true, "أبيض"
	}
	// جمود (stalemate) — تعادل، لا فائز.
	return true, ""
}

// ─── تحويل FEN <-> Board ────────────────────────────────────────────

// fenToBoard يبني Board كاملاً من سلسلة FEN قياسية (6 حقول): وضعية
// القطع، الدور، حقوق التبييت، مربّع en passant، ونصف/كامل النقلات
// (الحقلان الأخيران غير مُتتبَّعين داخلياً — لا قاعدة الخمسين نقلة ولا
// تكرار الوضعية مُطبَّقان هنا، بنفس ما كانت عليه النسخة التفاعلية
// الأصلية أصلاً).
func fenToBoard(fen string) (*Board, error) {
	fields := strings.Fields(strings.TrimSpace(fen))
	if len(fields) < 4 {
		return nil, fmt.Errorf("FEN يجب أن يحتوي 4 حقول على الأقل")
	}
	placement, active, castling, ep := fields[0], fields[1], fields[2], fields[3]

	ranks := strings.Split(placement, "/")
	if len(ranks) != 8 {
		return nil, fmt.Errorf("عدد الرتب في FEN يجب أن يكون 8")
	}

	b := &Board{EpSquare: -1}
	for i, rankStr := range ranks {
		rank := 7 - i // الرتبة الأولى في FEN هي rank8 (الأعلى)
		file := 0
		for _, r := range rankStr {
			if r >= '1' && r <= '8' {
				file += int(r - '0')
				continue
			}
			p := pieceFromRune(r)
			if p == Empty {
				return nil, fmt.Errorf("رمز قطعة غير صالح: %c", r)
			}
			if file > 7 {
				return nil, fmt.Errorf("رتبة FEN تتجاوز 8 أعمدة")
			}
			b.Squares[sq(file, rank)] = p
			file++
		}
		if file != 8 {
			return nil, fmt.Errorf("رتبة FEN لا تغطي 8 أعمدة بالضبط")
		}
	}

	switch active {
	case "w":
		b.Turn = true
	case "b":
		b.Turn = false
	default:
		return nil, fmt.Errorf("حقل الدور في FEN يجب أن يكون w أو b")
	}

	if castling != "-" {
		for _, c := range castling {
			switch c {
			case 'K':
				b.Castling[0] = true
			case 'Q':
				b.Castling[1] = true
			case 'k':
				b.Castling[2] = true
			case 'q':
				b.Castling[3] = true
			default:
				return nil, fmt.Errorf("رمز تبييت غير صالح: %c", c)
			}
		}
	}

	if ep != "-" {
		if len(ep) != 2 {
			return nil, fmt.Errorf("مربّع en passant غير صالح: %s", ep)
		}
		epFile := ep[0] - 'a'
		epRank := ep[1] - '1'
		if epFile > 7 || epRank > 7 {
			return nil, fmt.Errorf("مربّع en passant غير صالح: %s", ep)
		}
		b.EpSquare = sq(int(epFile), int(epRank))
	}

	return b, nil
}

// toFEN يُنتج سلسلة FEN من الحالة الحالية للرقعة. حقلا نصف/كامل
// النقلات ثابتان (0 1) لأنهما غير مُتتبَّعين داخلياً — راجع تعليق
// fenToBoard أعلاه لسبب ذلك.
func (b *Board) toFEN() string {
	var sb strings.Builder
	for rank := 7; rank >= 0; rank-- {
		empty := 0
		for file := 0; file < 8; file++ {
			p := b.Squares[sq(file, rank)]
			if p == Empty {
				empty++
				continue
			}
			if empty > 0 {
				sb.WriteString(strconv.Itoa(empty))
				empty = 0
			}
			sb.WriteRune(pieceToRune(p))
		}
		if empty > 0 {
			sb.WriteString(strconv.Itoa(empty))
		}
		if rank > 0 {
			sb.WriteByte('/')
		}
	}

	sb.WriteByte(' ')
	if b.Turn {
		sb.WriteByte('w')
	} else {
		sb.WriteByte('b')
	}

	sb.WriteByte(' ')
	castling := ""
	if b.Castling[0] {
		castling += "K"
	}
	if b.Castling[1] {
		castling += "Q"
	}
	if b.Castling[2] {
		castling += "k"
	}
	if b.Castling[3] {
		castling += "q"
	}
	if castling == "" {
		castling = "-"
	}
	sb.WriteString(castling)

	sb.WriteByte(' ')
	if b.EpSquare == -1 {
		sb.WriteByte('-')
	} else {
		sb.WriteString(b.EpSquare.String())
	}

	sb.WriteString(" 0 1")
	return sb.String()
}

// ─── Board & Move representation (منقول من المحرّك التفاعلي الأصلي) ─

type Piece byte

const (
	Empty Piece = iota
	WhitePawn
	WhiteKnight
	WhiteBishop
	WhiteRook
	WhiteQueen
	WhiteKing
	BlackPawn
	BlackKnight
	BlackBishop
	BlackRook
	BlackQueen
	BlackKing
)

func pieceFromRune(r rune) Piece {
	switch r {
	case 'P':
		return WhitePawn
	case 'N':
		return WhiteKnight
	case 'B':
		return WhiteBishop
	case 'R':
		return WhiteRook
	case 'Q':
		return WhiteQueen
	case 'K':
		return WhiteKing
	case 'p':
		return BlackPawn
	case 'n':
		return BlackKnight
	case 'b':
		return BlackBishop
	case 'r':
		return BlackRook
	case 'q':
		return BlackQueen
	case 'k':
		return BlackKing
	}
	return Empty
}

func pieceToRune(p Piece) rune {
	switch p {
	case WhitePawn:
		return 'P'
	case WhiteKnight:
		return 'N'
	case WhiteBishop:
		return 'B'
	case WhiteRook:
		return 'R'
	case WhiteQueen:
		return 'Q'
	case WhiteKing:
		return 'K'
	case BlackPawn:
		return 'p'
	case BlackKnight:
		return 'n'
	case BlackBishop:
		return 'b'
	case BlackRook:
		return 'r'
	case BlackQueen:
		return 'q'
	case BlackKing:
		return 'k'
	}
	return '.'
}

func isWhitePiece(p Piece) bool {
	return p >= WhitePawn && p <= WhiteKing
}
func isBlackPiece(p Piece) bool {
	return p >= BlackPawn && p <= BlackKing
}
func pieceColor(p Piece) int {
	if isWhitePiece(p) {
		return 0 // white
	}
	if isBlackPiece(p) {
		return 1 // black
	}
	return -1
}

type Square int // 0..63

func sq(file, rank int) Square { return Square(rank*8 + file) }
func (s Square) file() int     { return int(s) % 8 }
func (s Square) rank() int     { return int(s) / 8 }
func (s Square) String() string {
	return fmt.Sprintf("%c%d", 'a'+s.file(), s.rank()+1)
}

// Move in UCI long algebraic form, e.g. "e2e4", "e7e8q"
type Move struct {
	From  Square
	To    Square
	Promo Piece // Empty = none
}

// Parse a move string like "e2e4" or "e7e8q".
// whiteToMove indicates whose turn it is, to set promotion piece color.
func parseMove(moveStr string, whiteToMove bool) (Move, error) {
	if len(moveStr) < 4 {
		return Move{}, fmt.Errorf("invalid move: %s", moveStr)
	}
	fromFile := moveStr[0] - 'a'
	fromRank := moveStr[1] - '1'
	toFile := moveStr[2] - 'a'
	toRank := moveStr[3] - '1'
	if fromFile > 7 || fromRank > 7 || toFile > 7 || toRank > 7 {
		return Move{}, fmt.Errorf("invalid square in move: %s", moveStr)
	}
	from := sq(int(fromFile), int(fromRank))
	to := sq(int(toFile), int(toRank))
	promo := Empty
	if len(moveStr) == 5 {
		switch moveStr[4] {
		case 'q':
			if whiteToMove {
				promo = WhiteQueen
			} else {
				promo = BlackQueen
			}
		case 'r':
			if whiteToMove {
				promo = WhiteRook
			} else {
				promo = BlackRook
			}
		case 'b':
			if whiteToMove {
				promo = WhiteBishop
			} else {
				promo = BlackBishop
			}
		case 'n':
			if whiteToMove {
				promo = WhiteKnight
			} else {
				promo = BlackKnight
			}
		default:
			return Move{}, fmt.Errorf("invalid promotion piece: %c", moveStr[4])
		}
	}
	return Move{From: from, To: to, Promo: promo}, nil
}

// ---------- Board state ----------

type Board struct {
	Squares  [64]Piece
	Turn     bool   // true = white to move
	EpSquare Square // -1 if none
	Castling [4]bool // white kingside, white queenside, black kingside, black queenside
}

func NewBoard() *Board {
	b := &Board{}
	for i := 0; i < 8; i++ {
		b.Squares[sq(i, 1)] = WhitePawn
		b.Squares[sq(i, 6)] = BlackPawn
	}
	b.Squares[sq(0, 0)] = WhiteRook
	b.Squares[sq(7, 0)] = WhiteRook
	b.Squares[sq(1, 0)] = WhiteKnight
	b.Squares[sq(6, 0)] = WhiteKnight
	b.Squares[sq(2, 0)] = WhiteBishop
	b.Squares[sq(5, 0)] = WhiteBishop
	b.Squares[sq(3, 0)] = WhiteQueen
	b.Squares[sq(4, 0)] = WhiteKing

	b.Squares[sq(0, 7)] = BlackRook
	b.Squares[sq(7, 7)] = BlackRook
	b.Squares[sq(1, 7)] = BlackKnight
	b.Squares[sq(6, 7)] = BlackKnight
	b.Squares[sq(2, 7)] = BlackBishop
	b.Squares[sq(5, 7)] = BlackBishop
	b.Squares[sq(3, 7)] = BlackQueen
	b.Squares[sq(4, 7)] = BlackKing

	b.Turn = true
	b.EpSquare = -1
	b.Castling = [4]bool{true, true, true, true}
	return b
}

func (b *Board) copy() *Board {
	cp := &Board{}
	cp.Squares = b.Squares
	cp.Turn = b.Turn
	cp.EpSquare = b.EpSquare
	cp.Castling = b.Castling
	return cp
}

// Is square attacked by pieces of the given color?
func (b *Board) isAttacked(target Square, byWhite bool) bool {
	file, rank := target.file(), target.rank()
	if byWhite {
		if rank+1 < 8 && file-1 >= 0 && b.Squares[sq(file-1, rank+1)] == WhitePawn {
			return true
		}
		if rank+1 < 8 && file+1 < 8 && b.Squares[sq(file+1, rank+1)] == WhitePawn {
			return true
		}
	} else {
		if rank-1 >= 0 && file-1 >= 0 && b.Squares[sq(file-1, rank-1)] == BlackPawn {
			return true
		}
		if rank-1 >= 0 && file+1 < 8 && b.Squares[sq(file+1, rank-1)] == BlackPawn {
			return true
		}
	}
	knightMoves := []struct{ df, dr int }{{-2, -1}, {-2, 1}, {-1, -2}, {-1, 2}, {1, -2}, {1, 2}, {2, -1}, {2, 1}}
	for _, m := range knightMoves {
		nf, nr := file+m.df, rank+m.dr
		if nf >= 0 && nf < 8 && nr >= 0 && nr < 8 {
			p := b.Squares[sq(nf, nr)]
			if (byWhite && p == WhiteKnight) || (!byWhite && p == BlackKnight) {
				return true
			}
		}
	}
	kingMoves := []struct{ df, dr int }{{-1, -1}, {-1, 0}, {-1, 1}, {0, -1}, {0, 1}, {1, -1}, {1, 0}, {1, 1}}
	for _, m := range kingMoves {
		nf, nr := file+m.df, rank+m.dr
		if nf >= 0 && nf < 8 && nr >= 0 && nr < 8 {
			p := b.Squares[sq(nf, nr)]
			if (byWhite && p == WhiteKing) || (!byWhite && p == BlackKing) {
				return true
			}
		}
	}
	dirs := []struct{ df, dr int }{{-1, -1}, {-1, 0}, {-1, 1}, {0, -1}, {0, 1}, {1, -1}, {1, 0}, {1, 1}}
	for _, d := range dirs {
		for step := 1; step < 8; step++ {
			nf, nr := file+d.df*step, rank+d.dr*step
			if nf < 0 || nf >= 8 || nr < 0 || nr >= 8 {
				break
			}
			p := b.Squares[sq(nf, nr)]
			if p == Empty {
				continue
			}
			if (byWhite && (p == WhiteBishop || p == WhiteRook || p == WhiteQueen)) ||
				(!byWhite && (p == BlackBishop || p == BlackRook || p == BlackQueen)) {
				return true
			}
			break
		}
	}
	return false
}

// Is the current side's king in check?
func (b *Board) inCheck() bool {
	var kingSq Square
	for sq := Square(0); sq < 64; sq++ {
		p := b.Squares[sq]
		if (b.Turn && p == WhiteKing) || (!b.Turn && p == BlackKing) {
			kingSq = sq
			break
		}
	}
	return b.isAttacked(kingSq, !b.Turn) // attacked by opponent
}

// Generate pseudo-legal moves for a square
func (b *Board) pseudoMoves(from Square) []Move {
	p := b.Squares[from]
	if p == Empty {
		return nil
	}
	file, rank := from.file(), from.rank()
	var moves []Move

	add := func(to Square, promo Piece) {
		if to < 0 || to > 63 {
			return
		}
		target := b.Squares[to]
		if target != Empty && pieceColor(target) == pieceColor(p) {
			return
		}
		moves = append(moves, Move{From: from, To: to, Promo: promo})
	}

	switch p {
	case WhitePawn:
		if rank+1 < 8 && b.Squares[sq(file, rank+1)] == Empty {
			if rank+1 == 7 {
				add(sq(file, 7), WhiteQueen)
				add(sq(file, 7), WhiteRook)
				add(sq(file, 7), WhiteBishop)
				add(sq(file, 7), WhiteKnight)
			} else {
				add(sq(file, rank+1), Empty)
			}
			if rank == 1 && b.Squares[sq(file, 3)] == Empty {
				add(sq(file, 3), Empty)
			}
		}
		if file-1 >= 0 && b.Squares[sq(file-1, rank+1)] != Empty && isBlackPiece(b.Squares[sq(file-1, rank+1)]) {
			if rank+1 == 7 {
				add(sq(file-1, 7), WhiteQueen)
				add(sq(file-1, 7), WhiteRook)
				add(sq(file-1, 7), WhiteBishop)
				add(sq(file-1, 7), WhiteKnight)
			} else {
				add(sq(file-1, rank+1), Empty)
			}
		}
		if file+1 < 8 && b.Squares[sq(file+1, rank+1)] != Empty && isBlackPiece(b.Squares[sq(file+1, rank+1)]) {
			if rank+1 == 7 {
				add(sq(file+1, 7), WhiteQueen)
				add(sq(file+1, 7), WhiteRook)
				add(sq(file+1, 7), WhiteBishop)
				add(sq(file+1, 7), WhiteKnight)
			} else {
				add(sq(file+1, rank+1), Empty)
			}
		}
		if b.EpSquare != -1 && rank == 4 && (sq(file-1, 5) == b.EpSquare || sq(file+1, 5) == b.EpSquare) {
			add(b.EpSquare, Empty)
		}
	case BlackPawn:
		if rank-1 >= 0 && b.Squares[sq(file, rank-1)] == Empty {
			if rank-1 == 0 {
				add(sq(file, 0), BlackQueen)
				add(sq(file, 0), BlackRook)
				add(sq(file, 0), BlackBishop)
				add(sq(file, 0), BlackKnight)
			} else {
				add(sq(file, rank-1), Empty)
			}
			if rank == 6 && b.Squares[sq(file, 4)] == Empty {
				add(sq(file, 4), Empty)
			}
		}
		if file-1 >= 0 && b.Squares[sq(file-1, rank-1)] != Empty && isWhitePiece(b.Squares[sq(file-1, rank-1)]) {
			if rank-1 == 0 {
				add(sq(file-1, 0), BlackQueen)
				add(sq(file-1, 0), BlackRook)
				add(sq(file-1, 0), BlackBishop)
				add(sq(file-1, 0), BlackKnight)
			} else {
				add(sq(file-1, rank-1), Empty)
			}
		}
		if file+1 < 8 && b.Squares[sq(file+1, rank-1)] != Empty && isWhitePiece(b.Squares[sq(file+1, rank-1)]) {
			if rank-1 == 0 {
				add(sq(file+1, 0), BlackQueen)
				add(sq(file+1, 0), BlackRook)
				add(sq(file+1, 0), BlackBishop)
				add(sq(file+1, 0), BlackKnight)
			} else {
				add(sq(file+1, rank-1), Empty)
			}
		}
		if b.EpSquare != -1 && rank == 3 && (sq(file-1, 2) == b.EpSquare || sq(file+1, 2) == b.EpSquare) {
			add(b.EpSquare, Empty)
		}
	case WhiteKnight, BlackKnight:
		offsets := []struct{ df, dr int }{{-2, -1}, {-2, 1}, {-1, -2}, {-1, 2}, {1, -2}, {1, 2}, {2, -1}, {2, 1}}
		for _, o := range offsets {
			to := sq(file+o.df, rank+o.dr)
			if to >= 0 && to < 64 && (b.Squares[to] == Empty || pieceColor(b.Squares[to]) != pieceColor(p)) {
				add(to, Empty)
			}
		}
	case WhiteBishop, BlackBishop:
		dirs := []struct{ df, dr int }{{-1, -1}, {-1, 1}, {1, -1}, {1, 1}}
		for _, d := range dirs {
			for step := 1; step < 8; step++ {
				to := sq(file+d.df*step, rank+d.dr*step)
				if to < 0 || to > 63 {
					break
				}
				target := b.Squares[to]
				if target == Empty {
					add(to, Empty)
				} else {
					if pieceColor(target) != pieceColor(p) {
						add(to, Empty)
					}
					break
				}
			}
		}
	case WhiteRook, BlackRook:
		dirs := []struct{ df, dr int }{{-1, 0}, {1, 0}, {0, -1}, {0, 1}}
		for _, d := range dirs {
			for step := 1; step < 8; step++ {
				to := sq(file+d.df*step, rank+d.dr*step)
				if to < 0 || to > 63 {
					break
				}
				target := b.Squares[to]
				if target == Empty {
					add(to, Empty)
				} else {
					if pieceColor(target) != pieceColor(p) {
						add(to, Empty)
					}
					break
				}
			}
		}
	case WhiteQueen, BlackQueen:
		dirs := []struct{ df, dr int }{{-1, -1}, {-1, 0}, {-1, 1}, {0, -1}, {0, 1}, {1, -1}, {1, 0}, {1, 1}}
		for _, d := range dirs {
			for step := 1; step < 8; step++ {
				to := sq(file+d.df*step, rank+d.dr*step)
				if to < 0 || to > 63 {
					break
				}
				target := b.Squares[to]
				if target == Empty {
					add(to, Empty)
				} else {
					if pieceColor(target) != pieceColor(p) {
						add(to, Empty)
					}
					break
				}
			}
		}
	case WhiteKing, BlackKing:
		offsets := []struct{ df, dr int }{{-1, -1}, {-1, 0}, {-1, 1}, {0, -1}, {0, 1}, {1, -1}, {1, 0}, {1, 1}}
		for _, o := range offsets {
			to := sq(file+o.df, rank+o.dr)
			if to >= 0 && to < 64 && (b.Squares[to] == Empty || pieceColor(b.Squares[to]) != pieceColor(p)) {
				add(to, Empty)
			}
		}
		if b.Turn && p == WhiteKing && !b.inCheck() {
			if b.Castling[0] && b.Squares[sq(5, 0)] == Empty && b.Squares[sq(6, 0)] == Empty &&
				!b.isAttacked(sq(5, 0), false) && !b.isAttacked(sq(6, 0), false) && b.Squares[sq(7, 0)] == WhiteRook {
				add(sq(6, 0), Empty)
			}
			if b.Castling[1] && b.Squares[sq(3, 0)] == Empty && b.Squares[sq(2, 0)] == Empty && b.Squares[sq(1, 0)] == Empty &&
				!b.isAttacked(sq(3, 0), false) && !b.isAttacked(sq(2, 0), false) && b.Squares[sq(0, 0)] == WhiteRook {
				add(sq(2, 0), Empty)
			}
		}
		if !b.Turn && p == BlackKing && !b.inCheck() {
			if b.Castling[2] && b.Squares[sq(5, 7)] == Empty && b.Squares[sq(6, 7)] == Empty &&
				!b.isAttacked(sq(5, 7), true) && !b.isAttacked(sq(6, 7), true) && b.Squares[sq(7, 7)] == BlackRook {
				add(sq(6, 7), Empty)
			}
			if b.Castling[3] && b.Squares[sq(3, 7)] == Empty && b.Squares[sq(2, 7)] == Empty && b.Squares[sq(1, 7)] == Empty &&
				!b.isAttacked(sq(3, 7), true) && !b.isAttacked(sq(2, 7), true) && b.Squares[sq(0, 7)] == BlackRook {
				add(sq(2, 7), Empty)
			}
		}
	}
	return moves
}

// Generate all legal moves for the side to move
func (b *Board) LegalMoves() []Move {
	var legal []Move
	for sq := Square(0); sq < 64; sq++ {
		if (b.Turn && isWhitePiece(b.Squares[sq])) || (!b.Turn && isBlackPiece(b.Squares[sq])) {
			pseudo := b.pseudoMoves(sq)
			for _, m := range pseudo {
				cp := b.copy()
				cp.applyMoveOnBoard(m)
				if !cp.inCheck() {
					legal = append(legal, m)
				}
			}
		}
	}
	return legal
}

// Apply a move directly to the board (without legality check). Used internally.
func (b *Board) applyMoveOnBoard(m Move) {
	captured := b.Squares[m.To]

	p := b.Squares[m.From]
	b.Squares[m.To] = p
	b.Squares[m.From] = Empty
	b.EpSquare = -1

	if m.Promo != Empty {
		b.Squares[m.To] = m.Promo
	}

	if p == WhiteKing && m.From == sq(4, 0) && m.To == sq(6, 0) {
		b.Squares[sq(7, 0)] = Empty
		b.Squares[sq(5, 0)] = WhiteRook
	} else if p == WhiteKing && m.From == sq(4, 0) && m.To == sq(2, 0) {
		b.Squares[sq(0, 0)] = Empty
		b.Squares[sq(3, 0)] = WhiteRook
	} else if p == BlackKing && m.From == sq(4, 7) && m.To == sq(6, 7) {
		b.Squares[sq(7, 7)] = Empty
		b.Squares[sq(5, 7)] = BlackRook
	} else if p == BlackKing && m.From == sq(4, 7) && m.To == sq(2, 7) {
		b.Squares[sq(0, 7)] = Empty
		b.Squares[sq(3, 7)] = BlackRook
	}

	if (p == WhitePawn || p == BlackPawn) && m.To == b.EpSquare {
		if p == WhitePawn {
			b.Squares[sq(m.To.file(), m.To.rank()-1)] = Empty
		} else {
			b.Squares[sq(m.To.file(), m.To.rank()+1)] = Empty
		}
	}

	if p == WhitePawn && m.From.rank() == 1 && m.To.rank() == 3 {
		b.EpSquare = sq(m.From.file(), 2)
	}
	if p == BlackPawn && m.From.rank() == 6 && m.To.rank() == 4 {
		b.EpSquare = sq(m.From.file(), 5)
	}

	if p == WhiteKing {
		b.Castling[0] = false
		b.Castling[1] = false
	}
	if p == BlackKing {
		b.Castling[2] = false
		b.Castling[3] = false
	}
	if p == WhiteRook && m.From == sq(0, 0) {
		b.Castling[1] = false
	}
	if p == WhiteRook && m.From == sq(7, 0) {
		b.Castling[0] = false
	}
	if p == BlackRook && m.From == sq(0, 7) {
		b.Castling[3] = false
	}
	if p == BlackRook && m.From == sq(7, 7) {
		b.Castling[2] = false
	}
	if captured == WhiteRook {
		if m.To == sq(0, 0) {
			b.Castling[1] = false
		}
		if m.To == sq(7, 0) {
			b.Castling[0] = false
		}
	}
	if captured == BlackRook {
		if m.To == sq(0, 7) {
			b.Castling[3] = false
		}
		if m.To == sq(7, 7) {
			b.Castling[2] = false
		}
	}

	b.Turn = !b.Turn
}

// Apply a move after legality check. Returns error if illegal.
func (b *Board) ApplyMove(moveStr string) error {
	m, err := parseMove(moveStr, b.Turn)
	if err != nil {
		return err
	}
	p := b.Squares[m.From]
	if p == Empty || (b.Turn && !isWhitePiece(p)) || (!b.Turn && !isBlackPiece(p)) {
		return fmt.Errorf("no piece to move at %v", m.From)
	}
	legal := b.LegalMoves()
	found := false
	for _, lm := range legal {
		if lm.From == m.From && lm.To == m.To && lm.Promo == m.Promo {
			found = true
			break
		}
	}
	if !found {
		return fmt.Errorf("illegal move: %s", moveStr)
	}
	b.applyMoveOnBoard(m)
	return nil
}

// ---------- SVG Generation ----------
//
// ⚠️ إصلاح جوهري (كان يكسر كل صورة فيها lastMove، أي كل نقلة بعد بداية
// اللعبة): كانت السمة "class" تُكتب مرتين على نفس عنصر <rect> عند
// التظليل (class="light" class="highlight") — XML/SVG لا يسمح بتكرار
// نفس السمة على نفس العنصر إطلاقاً. الناتج SVG غير صالح: sharp
// (libvips/librsvg) يفشل بتحويله PNG فيرمي خطأ، فيتراجع الكود لإرسال
// SVG الخام — وهو نفسه غير صالح فيفشل رفعه على ميسنجر بصمت (يرجع بدون
// metadata/ids بلا استثناء JS). الحل: قيمة واحدة لسمة class تجمع
// class اللون + class التظليل مفصولة بمسافة (class="dark highlight")
// وهذا صالح تماماً في CSS/SVG. كما تم نقل حساب مربّعي lastMove خارج
// اللوب المزدوج بدل إعادة تحليل النقلة 64 مرة.
//
// تحسينات بصرية: رقعة أبيض/أخضر بدل البني التقليدي، ألوان قطع صريحة
// (أبيض بحد أسود، أسود بحد أبيض) بدل الاعتماد فقط على شكل الرمز
// اليونيكود ليتضح التباين على الخلفية الخضراء، ظل خفيف للقطع لعمق
// بصري، ومربعات أكبر (60px بدل 50px) لدقة أعلى.
//
// ملاحظة نشر: رموز الشطرنج اليونيكود (♔♕♜...) تحتاج خطاً يحتوي رموز
// Chess Symbols مثبَّتاً في صورة الحاوية (مثل DejaVu Sans أو Noto Sans
// Symbols2) ليعرضها librsvg بشكل صحيح — تأكد من apt install
// fonts-dejavu-core (أو ما يعادله) في الـ Dockerfile.
func boardToSVG(b *Board, lastMove string) string {
	const squareSize = 60
	const margin = 26
	width := margin + 8*squareSize + margin
	height := margin + 8*squareSize + margin

	// مربّعا آخر نقلة (from/to) — يُحسبان مرة واحدة فقط، لا 64 مرة.
	highlightFrom, highlightTo := Square(-1), Square(-1)
	if lastMove != "" && len(lastMove) >= 4 {
		if m, err := parseMove(lastMove, true); err == nil {
			highlightFrom, highlightTo = m.From, m.To
		}
	}

	var sb strings.Builder
	sb.WriteString(`<svg xmlns="http://www.w3.org/2000/svg" width="` + fmt.Sprint(width) + `" height="` + fmt.Sprint(height) + `" viewBox="0 0 ` + fmt.Sprint(width) + ` ` + fmt.Sprint(height) + `">
<style>
  .coords { font-family: 'DejaVu Sans', 'Noto Sans', sans-serif; font-size: 15px; font-weight: 600; fill: #2e4a2e; text-anchor: middle; dominant-baseline: middle; }
  .light { fill: #ffffff; }
  .dark  { fill: #4c8c3c; }
  .highlight { fill: #f6f66b; fill-opacity: 0.65; }
  .board-border { fill: none; stroke: #2e4a2e; stroke-width: 2; }
  .piece-text { font-family: 'DejaVu Sans', 'Noto Sans Symbols2', 'Segoe UI Symbol', sans-serif; font-size: 46px; text-anchor: middle; dominant-baseline: central; paint-order: stroke fill; filter: drop-shadow(0 1px 1.5px rgba(0,0,0,0.35)); }
  .piece-white { fill: #ffffff; stroke: #1a1a1a; stroke-width: 2px; }
  .piece-black { fill: #1a1a1a; stroke: #ffffff; stroke-width: 1.2px; }
</style>`)

	for rank := 0; rank < 8; rank++ {
		for file := 0; file < 8; file++ {
			x := margin + file*squareSize
			y := margin + (7-rank)*squareSize
			colorClass := "light"
			if (file+rank)%2 != 0 {
				colorClass = "dark"
			}
			curSq := sq(file, rank)
			classAttr := colorClass
			if curSq == highlightFrom || curSq == highlightTo {
				classAttr = colorClass + " highlight"
			}
			sb.WriteString(fmt.Sprintf(`<rect x="%d" y="%d" width="%d" height="%d" class="%s"/>`, x, y, squareSize, squareSize, classAttr))
		}
	}

	// إطار خارجي حول الرقعة لتحسين المظهر.
	sb.WriteString(fmt.Sprintf(`<rect x="%d" y="%d" width="%d" height="%d" class="board-border"/>`, margin, margin, 8*squareSize, 8*squareSize))

	pieceSymbols := map[Piece]rune{
		WhiteKing: '♔', WhiteQueen: '♕', WhiteRook: '♖', WhiteBishop: '♗', WhiteKnight: '♘', WhitePawn: '♙',
		BlackKing: '♚', BlackQueen: '♛', BlackRook: '♜', BlackBishop: '♝', BlackKnight: '♞', BlackPawn: '♟',
	}
	for rank := 0; rank < 8; rank++ {
		for file := 0; file < 8; file++ {
			p := b.Squares[sq(file, rank)]
			if p == Empty {
				continue
			}
			x := margin + file*squareSize + squareSize/2
			y := margin + (7-rank)*squareSize + squareSize/2 + 2
			colorClass := "piece-black"
			if isWhitePiece(p) {
				colorClass = "piece-white"
			}
			sb.WriteString(fmt.Sprintf(`<text x="%d" y="%d" class="piece-text %s">%c</text>`, x, y, colorClass, pieceSymbols[p]))
		}
	}

	for file := 0; file < 8; file++ {
		x := margin + file*squareSize + squareSize/2
		yTop := margin - 12
		sb.WriteString(fmt.Sprintf(`<text x="%d" y="%d" class="coords">%c</text>`, x, yTop, 'a'+file))
		yBottom := margin + 8*squareSize + 12
		sb.WriteString(fmt.Sprintf(`<text x="%d" y="%d" class="coords">%c</text>`, x, yBottom, 'a'+file))
	}
	for rank := 0; rank < 8; rank++ {
		y := margin + (7-rank)*squareSize + squareSize/2
		xLeft := margin - 12
		sb.WriteString(fmt.Sprintf(`<text x="%d" y="%d" class="coords">%d</text>`, xLeft, y, rank+1))
		xRight := margin + 8*squareSize + 12
		sb.WriteString(fmt.Sprintf(`<text x="%d" y="%d" class="coords">%d</text>`, xRight, y, rank+1))
	}

	sb.WriteString(`</svg>`)
	return sb.String()
}

// ---------- UCI / Stockfish Integration ----------

type uciEngine struct {
	cmd    *exec.Cmd
	stdin  io.WriteCloser
	stdout *bufio.Scanner
}

func startStockfish(ctx context.Context) (*uciEngine, error) {
	cmd := exec.CommandContext(ctx, stockfishPath())
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("تعذّر تشغيل stockfish: %w", err)
	}
	e := &uciEngine{cmd: cmd, stdin: stdin, stdout: bufio.NewScanner(stdout)}
	e.send("uci")
	if !e.waitFor("uciok") {
		return nil, fmt.Errorf("stockfish لم يرد بـ uciok — %s", truncate(stderr.String(), 200))
	}
	e.send("isready")
	if !e.waitFor("readyok") {
		return nil, fmt.Errorf("stockfish لم يرد بـ readyok — %s", truncate(stderr.String(), 200))
	}
	return e, nil
}

func (e *uciEngine) send(cmd string) {
	io.WriteString(e.stdin, cmd+"\n")
}

func (e *uciEngine) waitFor(target string) bool {
	for e.stdout.Scan() {
		if strings.HasPrefix(e.stdout.Text(), target) {
			return true
		}
	}
	return false
}

func (e *uciEngine) setSkillLevel(level int) {
	e.send(fmt.Sprintf("setoption name Skill Level value %d", level))
	e.send("isready")
	e.waitFor("readyok")
}

func (e *uciEngine) bestMoveForFEN(fen string, movetimeMS int) (string, error) {
	e.send("position fen " + fen)
	e.send(fmt.Sprintf("go movetime %d", movetimeMS))
	for e.stdout.Scan() {
		line := e.stdout.Text()
		if strings.HasPrefix(line, "bestmove") {
			parts := strings.Fields(line)
			if len(parts) >= 2 && parts[1] != "(none)" {
				return parts[1], nil
			}
			return "", fmt.Errorf("stockfish لم يجد نقلة (لا توجد نقلات قانونية)")
		}
	}
	return "", fmt.Errorf("لم يصل رد bestmove من stockfish")
}

func (e *uciEngine) close() {
	e.send("quit")
	_ = e.cmd.Wait()
}

// difficultyToEngineParams يحوّل مستوى صعوبة يطابق Stockfish UCI "Skill
// Level" مباشرة (0-20، بلا وسيط) إلى (Skill Level, مهلة تفكير بالميلي
// ثانية). مهلة التفكير تتدرّج خطياً بين 300ms عند المستوى 0 و2000ms عند
// المستوى 20، حتى تصبح المستويات العليا أقوى فعلياً (Skill Level وحده
// لا يكفي — Stockfish يحتاج وقت تفكير أطول ليستغل قوته الكاملة).
func difficultyToEngineParams(difficulty int) (skill int, movetimeMS int) {
	if difficulty < 0 {
		difficulty = 0
	}
	if difficulty > 20 {
		difficulty = 20
	}
	skill = difficulty
	movetimeMS = 300 + (difficulty*1700)/20 // 300ms..2000ms
	return skill, movetimeMS
}

// stockfishBestMove يشغّل Stockfish كعملية فرعية كاملة (تشغيل → طلب
// نقلة واحدة → إغلاق) لكل استدعاء — أبسط وأكثر أماناً من الاحتفاظ
// بعملية طويلة العمر عبر طلبات HTTP متزامنة متعددة، على حساب فترة بدء
// تشغيل صغيرة إضافية لكل نقلة (مقبولة هنا مقابل البساطة).
func stockfishBestMove(ctx context.Context, fen string, difficulty int) (string, error) {
	engine, err := startStockfish(ctx)
	if err != nil {
		return "", err
	}
	defer engine.close()

	skill, movetimeMS := difficultyToEngineParams(difficulty)
	engine.setSkillLevel(skill)
	return engine.bestMoveForFEN(fen, movetimeMS)
}
