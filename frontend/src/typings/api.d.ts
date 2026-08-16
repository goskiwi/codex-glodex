/**
 * Namespace Api
 *
 * All backend api type
 */
declare namespace Api {
  namespace Common {
    /** common params of paginating */
    interface PaginatingCommonParams {
      /** current page number */
      page?: number;
      number: number;
      /** page size */
      size?: number;
      /** total count */
      totalElements: number;
    }

    /** common params of paginating query list data */
    interface PaginatingQueryRecord<T = any> extends PaginatingCommonParams {
      data: T[];
      content: T[];
    }

    /** common search params of table */
    type CommonSearchParams = Pick<Common.PaginatingCommonParams, 'page' | 'size'>;
  }

  /**
   * namespace Auth
   *
   * backend api module: "auth"
   */
  namespace Auth {
    interface UserInfo {
      id: number;
      username: string;
      role: 'USER' | 'ADMIN';
      orgTags: string[];
      primaryOrg: string;
    }
  }

  /**
   * namespace Route
   *
   * backend api module: "route"
   */
  namespace Route {
    type ElegantConstRoute = import('@elegant-router/types').ElegantConstRoute;

    interface MenuRoute extends ElegantConstRoute {
      id: string;
    }

    interface UserRoute {
      routes: MenuRoute[];
      home: import('@elegant-router/types').LastLevelRouteKey;
    }
  }

  namespace Admin {
    interface WindowLimit {
      max: number;
      windowSeconds: number;
    }

    interface DualWindowLimit {
      minuteMax: number;
      minuteWindowSeconds: number;
      dayMax: number;
      dayWindowSeconds: number;
    }

    interface TokenBudgetLimit {
      minuteMax: number;
      minuteWindowSeconds: number;
      dayMax: number;
      dayWindowSeconds: number;
    }

    interface RateLimitSettings {
      chatMessage: WindowLimit;
      llmGlobalToken: TokenBudgetLimit;
      embeddingUploadToken: TokenBudgetLimit;
      embeddingQueryRequest: DualWindowLimit;
      embeddingQueryGlobalToken: TokenBudgetLimit;
    }

    interface UsageTrendPoint {
      day: string;
      chatRequestCount: number;
      llmUsedTokens: number;
      llmRequestCount: number;
      embeddingUsedTokens: number;
      embeddingRequestCount: number;
    }

    interface UsageRankingItem {
      userId: string;
      username: string;
      scope: 'llm' | 'embedding';
      usedTokens: number;
      limitTokens: number;
      remainingTokens: number;
      requestCount: number;
    }

    interface UsageAlert {
      level: 'critical' | 'warning';
      userId: string;
      username: string;
      scope: 'llm' | 'embedding';
      usedTokens: number;
      limitTokens: number;
      remainingTokens: number;
      requestCount: number;
      usageRatio: number;
      message: string;
    }

    interface UsageOverview {
      days: number;
      today: UsageTrendPoint;
      trends: UsageTrendPoint[];
      llmRankings: UsageRankingItem[];
      embeddingRankings: UsageRankingItem[];
      alerts: UsageAlert[];
    }
  }

  namespace Chat {
    type GenerationStatus = 'STREAMING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';

    interface ReferenceEvidence {
      fileMd5: string;
      fileName: string;
      pageNumber?: number | null;
      anchorText?: string | null;
      retrievalMode?: 'HYBRID' | 'TEXT_ONLY' | null;
      retrievalLabel?: string | null;
      retrievalQuery?: string | null;
      matchedChunkText?: string | null;
      evidenceSnippet?: string | null;
      score?: number | null;
      chunkId?: number | null;
    }

    interface Input {
      message: string;
      conversationId?: string;
    }

    interface Output {
      chunk: string;
    }

    interface AgentToolEvent {
      id?: string;
      tool: string;
      status: 'executing' | 'success' | 'failed';
      timestamp?: number;
    }

    interface Conversation {
      conversationId: string;
    }

    interface Message {
      role: 'user' | 'assistant';
      content: string;
      status?: 'pending' | 'loading' | 'finished' | 'error';
      timestamp?: string;
      conversationId?: string;
      generationId?: string;
      username?: string;
      referenceMappings?: Record<string, ReferenceEvidence>;
      toolEvents?: AgentToolEvent[];
      feedbackRating?: 'good' | 'bad';
    }

    interface Token {
      cmdToken: string;
    }

    interface GenerationSnapshot {
      generationId: string;
      userId: string;
      conversationId: string;
      question: string;
      status: GenerationStatus;
      content: string;
      createdAt: string;
      updatedAt: string;
      errorMessage?: string | null;
      referenceMappings?: Record<string, ReferenceEvidence>;
    }

    interface ConversationSession {
      id: number;
      conversationId: string;
      title: string;
      status: 'ACTIVE' | 'ARCHIVED';
      createdAt: string;
      updatedAt: string;
    }
  }

  namespace Document {
    interface DownloadResponse {
      fileName: string;
      downloadUrl: string;
      fileSize: number;
      fileMd5?: string;
    }

    interface ReferenceDetailResponse extends Chat.ReferenceEvidence {
      referenceNumber: number;
    }
  }
}
