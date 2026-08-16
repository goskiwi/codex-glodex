import type { AnyColor } from 'colord';
import { getHex } from '../shared';
import type { ColorPaletteNumber } from '../types';
import { getAntDColorPalette } from './antd';

/**
 * get color palette by provided color
 *
 * @param color
 */
export function getColorPalette(color: AnyColor) {
  const colorMap = new Map<ColorPaletteNumber, string>();
  const colors = getAntDColorPalette(getHex(color));
  const colorNumbers: ColorPaletteNumber[] = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950];
  colorNumbers.forEach((number, index) => colorMap.set(number, colors[index]));

  return colorMap;
}

/**
 * get color palette color by number
 *
 * @param color the provided color
 * @param number the color palette number
 */
export function getPaletteColorByNumber(color: AnyColor, number: ColorPaletteNumber) {
  const colorMap = getColorPalette(color);

  return colorMap.get(number as ColorPaletteNumber)!;
}
