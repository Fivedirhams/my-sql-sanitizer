-- =============================================================================
-- CRM Database Sample (Business-oriented)
-- Simulates typical Russian corporate client management system
-- =============================================================================

CREATE DATABASE IF NOT EXISTS `business_crm`;
USE `business_crm`;

-- Companies table
DROP TABLE IF EXISTS `companies`;
CREATE TABLE `companies` (
    `CompanyID` INT AUTO_INCREMENT PRIMARY KEY,
    `INN` VARCHAR(10) NOT NULL,
    `KPP` VARCHAR(9) DEFAULT NULL,
    `OGRN` VARCHAR(13) DEFAULT NULL,
    `CompanyName` VARCHAR(255) NOT NULL,
    `LegalAddress` TEXT DEFAULT NULL,
    `City` VARCHAR(100) DEFAULT NULL,
    `Phone` VARCHAR(20) DEFAULT NULL,
    `Email` VARCHAR(255) DEFAULT NULL,
    `Website` VARCHAR(255) DEFAULT NULL,
    `Industry` VARCHAR(100) DEFAULT NULL,
    `Status` ENUM('active','inactive','bankrupt','under_review') DEFAULT 'active',
    `CreatedDate` DATE DEFAULT NULL,
    INDEX `idx_company_name` (`CompanyName`),
    INDEX `idx_city` (`City`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `companies` (`CompanyID`, `INN`, `KPP`, `OGRN`, `CompanyName`, `LegalAddress`, `City`, `Phone`, `Email`, `Website`, `Industry`, `Status`, `CreatedDate`) VALUES
(1, '7707083893', '770701001', '1027700132195', 'ООО ТехноПром Сервис', 'ул. Ленина, д. 45, офис 302', 'Москва', '+7 (495) 123-45-67', 'info@technoprom.ru', 'https://technoprom.ru', 'IT-консалтинг', 'active', '2021-03-15'),
(2, '7710263421', '771001001', '1187746123456', 'АО АльфаЛогистик', 'пр. Мира, д. 12', 'Санкт-Петербург', '+7 (812) 987-65-43', 'office@alfalog.ru', 'https://alfalog.ru', 'Транспорт', 'active', '2019-11-02'),
(3, '5024018932', '502401001', '1165024018932', 'Иван Петров ИП', 'ул. Гагарина, д. 8', 'Казань', '+7 (843) 555-12-34', 'ivan.petrov@mail.ru', NULL, 'Розничная торговля', 'inactive', '2022-07-20'),
(4, '7701234567', '770101001', '1047712345678', 'Завод СинтезПласт', 'ул. Промышленная, д. 15', 'Нижний Новгород', '+7 (831) 444-33-22', 'sales@synthplast.com', 'https://synthplast.com', 'Производство пластика', 'active', '2018-05-10'),
(5, '5036012345', '503601001', '1175036012345', 'ФармаКонцепт', 'бульвар Строителей, д. 2', 'Екатеринбург', '+7 (343) 222-11-00', 'procurement@pharmaconcept.ru', 'https://pharmaconcept.ru', 'Фармация', 'under_review', '2023-01-18');

-- Contacts / Clients
DROP TABLE IF EXISTS `contacts`;
CREATE TABLE `contacts` (
    `ContactID` INT AUTO_INCREMENT PRIMARY KEY,
    `CompanyID` INT DEFAULT NULL,
    `FirstName` VARCHAR(100) NOT NULL,
    `LastName` VARCHAR(100) NOT NULL,
    `MiddleName` VARCHAR(100) DEFAULT NULL,
    `JobTitle` VARCHAR(150) DEFAULT NULL,
    `PersonalEmail` VARCHAR(255) DEFAULT NULL,
    `DirectPhone` VARCHAR(20) DEFAULT NULL,
    `PreferredCity` VARCHAR(100) DEFAULT NULL,
    `Notes` TEXT DEFAULT NULL,
    `LastContactDate` DATE DEFAULT NULL,
    `LeadSource` ENUM('web', 'referral', 'cold_call', 'expo', 'partner') DEFAULT NULL,
    `Status` ENUM('new','qualified','negotiation','won','lost','on_hold') DEFAULT 'new',
    FOREIGN KEY (`CompanyID`) REFERENCES `companies`(`CompanyID`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `contacts` (`ContactID`, `CompanyID`, `FirstName`, `LastName`, `MiddleName`, `JobTitle`, `PersonalEmail`, `DirectPhone`, `PreferredCity`, `Notes`, `LastContactDate`, `LeadSource`, `Status`) VALUES
(1, 1, 'Алексей', 'Смирнов', 'Викторович', 'Генеральный директор', 'a.smirnov@technoprom.ru', '+7 (495) 123-45-68', 'Москва', 'Принимает решения по IT бюджету', '2024-01-15', 'referral', 'qualified'),
(2, 1, 'Мария', 'Кузнецова', 'Андреевна', 'Руководитель закупок', 'm.kuznetsova@technoprom.ru', '+7 (495) 123-45-69', 'Москва', 'Контролирует тендеры', '2024-02-10', 'expo', 'negotiation'),
(3, 2, 'Дмитрий', 'Волков', 'Сергеевич', 'Коммерческий директор', 'd.volkov@alfalog.ru', '+7 (812) 987-65-44', 'Санкт-Петербург', 'Работает по контрактам РФ', '2024-03-01', 'web', 'won'),
(4, 2, 'Елена', 'Соколова', 'Игоревна', 'Финансовый аналитик', 'e.sokolova@alfalog.ru', '+7 (812) 987-65-45', 'Санкт-Петербург', 'Требуется кросс-тренинг', '2024-01-20', 'partner', 'on_hold'),
(5, 3, 'Петр', 'Иванов', NULL, 'Владелец бизнеса', 'p.ivanov@gmail.com', '+7 (843) 555-12-35', 'Казань', 'Ищет поставщика сырья', '2023-11-05', 'cold_call', 'lost'),
(6, 4, 'Ольга', 'Новикова', 'Павловна', 'Начальник склада', 'o.novikova@synthplast.com', '+7 (831) 444-33-23', 'Нижний Новгород', 'Часто запрашивает образцы', '2024-02-28', 'referral', 'qualified'),
(7, 5, 'Артём', 'Лебедев', 'Романович', 'Главный врач', 'a.lebedev@pharmaconcept.ru', '+7 (343) 222-11-01', 'Екатеринбург', 'Подпись договора возможна Q3', '2024-03-12', 'web', 'negotiation');

-- Deals & Contracts
DROP TABLE IF EXISTS `deals`;
CREATE TABLE `deals` (
    `DealID` INT AUTO_INCREMENT PRIMARY KEY,
    `ContactID` INT DEFAULT NULL,
    `CompanyID` INT DEFAULT NULL,
    `ContractNumber` VARCHAR(50) DEFAULT NULL,
    `DealAmountUSD` DECIMAL(15,2) DEFAULT NULL,
    `Currency` VARCHAR(3) DEFAULT 'USD',
    `StartDate` DATE DEFAULT NULL,
    `EndDate` DATE DEFAULT NULL,
    `Terms` ENUM('prepaid','net30','net60','net90','escrow') DEFAULT 'net30',
    `ManagerID` INT DEFAULT NULL,
    `Stage` ENUM('prospecting','proposal','negotiation','closed_won','closed_lost') DEFAULT 'prospecting',
    `Description` TEXT DEFAULT NULL,
    FOREIGN KEY (`ContactID`) REFERENCES `contacts`(`ContactID`),
    FOREIGN KEY (`CompanyID`) REFERENCES `companies`(`CompanyID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `deals` (`DealID`, `ContactID`, `CompanyID`, `ContractNumber`, `DealAmountUSD`, `Currency`, `StartDate`, `EndDate`, `Terms`, `ManagerID`, `Stage`, `Description`) VALUES
(1, 1, 1, 'TP-2024-001-A', 125000.00, 'USD', '2024-01-10', '2025-01-10', 'net30', 101, 'closed_won', 'Лицензии ПО на 50 сотрудников'),
(2, 2, 1, 'TP-2024-002-B', 45000.00, 'RUB', '2024-02-15', '2024-12-31', 'net60', 102, 'negotiation', 'Аудит безопасности infra'),
(3, 3, 2, 'AL-LOG-882', 890000.00, 'USD', '2024-03-01', '2026-03-01', 'prepaid', 103, 'closed_won', 'Аутсорсинг логистики по ЦФО'),
(4, 6, 4, 'SP-MAIN-44', 2100000.00, 'RUB', '2023-12-01', '2025-12-01', 'escrow', 101, 'proposal', 'Поставка полимеров для линии №4'),
(5, 7, 5, 'PC-DISTRICT-09', 340000.00, 'EUR', '2024-06-01', '2024-06-30', 'net90', 105, 'prospecting', 'Реагенты для лаборатории');

-- Payments / Invoices
DROP TABLE IF EXISTS `payments`;
CREATE TABLE `payments` (
    `PaymentID` INT AUTO_INCREMENT PRIMARY KEY,
    `DealID` INT DEFAULT NULL,
    `InvoiceNumber` VARCHAR(50) NOT NULL,
    `PayAmount` DECIMAL(15,2) DEFAULT NULL,
    `PayCurrency` VARCHAR(3) DEFAULT 'RUB',
    `PayDate` DATE DEFAULT NULL,
    `Method` ENUM('bank_transfer','crypto','card','cash','advance_check') DEFAULT NULL,
    `Status` ENUM('pending','paid','overdue','cancelled') DEFAULT 'pending',
    FOREIGN KEY (`DealID`) REFERENCES `deals`(`DealID`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `payments` (`PaymentID`, `DealID`, `InvoiceNumber`, `PayAmount`, `PayCurrency`, `PayDate`, `Method`, `Status`) VALUES
(1, 1, 'INV-TP-001', 100000.00, 'USD', '2024-02-05', 'bank_transfer', 'paid'),
(2, 3, 'INV-AL-882-P1', 445000.00, 'USD', '2024-03-05', 'bank_transfer', 'paid'),
(3, 4, 'INV-SP-44-P1', 1260000.00, 'RUB', '2023-12-15', 'advance_check', 'paid'),
(4, 2, 'INV-TP-002', 45000.00, 'RUB', '2024-02-20', 'bank_transfer', 'pending'),
(5, 5, 'INV-PC-09', 340000.00, 'EUR', '2024-06-10', 'bank_transfer', 'pending'),
(6, 1, 'INV-TP-001-FINAL', 25000.00, 'USD', '2024-01-15', 'crypto', 'cancelled');

SELECT 'CRM_SAMPLE_LOADED_OK' AS status;
