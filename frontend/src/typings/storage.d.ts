/** The storage namespace */
declare namespace StorageType {
  interface Session {
    /** The theme color */
    themeColor: string;
    // /**
    //  * the theme settings
    //  */
    // themeSettings: App.Theme.ThemeSetting;
  }

  interface Local {
    /** The i18n language */
    lang: App.I18n.LangType;
    /** The token */
    token: string;
    /** The theme color */
    themeColor: string;
    /** The dark mode */
    darkMode: boolean;
    /** The theme settings */
    themeSettings: App.Theme.ThemeSetting;
    /**
     * The override theme flags
     *
     * The value is the build time of the project
     */
    overrideThemeFlag: string;
    /** Remembered local username; passwords are never persisted. */
    rememberedLogin: {
      userName: string;
    };
    /** Saved Glodex shopping research candidates */
    glodexCartItemsV2: import('@/store/modules/cart').GlodexCartItem[];
    /** Saved Glodex product favorites */
    glodexFavoriteItemsV2: import('@/store/modules/cart').GlodexCartItem[];
  }
}
